"""Shell detection, default selection and name resolution."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from typing import Literal

from envelope.config.settings import CustomShell
from envelope.log import logger
from envelope.shells import gitbash
from envelope.shells.ids import BASH_FAMILY, BUILTIN_SHELLS, POWERSHELLS, canonical_shell_id
from envelope.shells.resolve import find_executable, first_existing, in_system32, windows_dirs

PROBE_BUDGET_S = 5.0

Platform = Literal["linux", "darwin", "win32"]


def current_platform() -> Platform:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


@dataclass
class Shell:
    id: str
    kind: Literal["builtin", "custom"]
    path: str | None
    available: bool
    version: str | None = None
    reason: str | None = None
    language_mode: str | None = None
    execution_policy: str | None = None
    git_root: str | None = None
    custom: CustomShell | None = None

    @property
    def family(self) -> Literal["posix", "powershell", "cmd", "fish", "custom"]:
        if self.kind == "custom":
            return "custom"
        if self.id in BASH_FAMILY:
            return "posix"
        if self.id in POWERSHELLS:
            return "powershell"
        if self.id == "cmd":
            return "cmd"
        return "fish"

    def describe(self) -> str:
        return f"{self.id} {self.version}" if self.version else self.id

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "available": self.available,
            "path": self.path,
            "version": self.version,
        }
        if self.reason:
            data["reason"] = self.reason
        if self.language_mode:
            data["language_mode"] = self.language_mode
        if self.execution_policy:
            data["execution_policy"] = self.execution_policy
        if self.git_root:
            data["git_root"] = self.git_root
        return data


class ShellError(Exception):
    """The requested shell is unknown or unavailable (a tool-level error)."""


# --- probes -------------------------------------------------------------------

ProbeRunner = Callable[[Sequence[str], float], tuple[int, str]]
"""(argv, timeout) -> (exit code, stdout). Raises TimeoutError or OSError."""


def run_probe(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    windows = sys.platform == "win32"
    flags = 0x08000000 if windows else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=not windows,
        creationflags=flags,
    )
    try:
        out, _ = proc.communicate(timeout=max(timeout, 0.01))
    except subprocess.TimeoutExpired:
        try:
            if windows:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise TimeoutError from None
    return proc.returncode, out.decode("utf-8", "replace")


_PS_PROBE = (
    '"$($PSVersionTable.PSVersion)|$($ExecutionContext.SessionState.LanguageMode)|'
    '$(Get-ExecutionPolicy)"'
)


def probe_argv(shell_id: str, path: str) -> list[str]:
    if shell_id in POWERSHELLS:
        return [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PS_PROBE]
    if shell_id == "cmd":
        return [path, "/d", "/c", "ver"]
    if shell_id == "sh":
        return [path, "-c", "echo ok"]
    return [path, "--version"]


def parse_probe(shell: Shell, code: int, out: str) -> None:
    text = out.strip()
    if shell.id in POWERSHELLS:
        parts = text.splitlines()[-1].split("|") if text else []
        if code != 0 or len(parts) != 3:
            shell.available = False
            shell.reason = f"probe failed (exit {code}): {text[:200]}"
            return
        shell.version, shell.language_mode, shell.execution_policy = (p.strip() for p in parts)
        return
    if code != 0:
        shell.available = False
        shell.reason = f"probe failed (exit {code}): {text[:200]}"
        return
    if shell.id == "sh":
        return
    if shell.id == "cmd":
        match = re.search(r"Version\s+([\d.]+)", text)
    else:
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text.splitlines()[0] if text else "")
    shell.version = match.group(1) if match else None


# --- candidate discovery ------------------------------------------------------


@dataclass
class HostInfo:
    platform: Platform
    environ: Mapping[str, str]
    is_file: Callable[[str], bool] = os.path.isfile
    registry: Callable[[], list[str]] = gitbash.read_registry_install_paths
    login_shell: str | None = None


def _candidates(host: HostInfo) -> list[Shell]:
    env = host.environ
    path_env = env.get("PATH") or env.get("Path")
    windows = host.platform == "win32"
    shells: list[Shell] = []
    git = gitbash.discover(env, is_file=host.is_file, registry=host.registry) if windows else None

    def missing(shell_id: str, reason: str = "not found") -> Shell:
        return Shell(shell_id, "builtin", None, False, reason=reason)

    for shell_id in BUILTIN_SHELLS:
        if shell_id == "git-bash":
            if not windows:
                shells.append(missing(shell_id, "only on Windows"))
            elif git is None:
                shells.append(missing(shell_id, "Git for Windows not found"))
            else:
                shells.append(Shell(shell_id, "builtin", git.bash, True, git_root=git.root))
            continue
        if shell_id in ("powershell", "cmd") and not windows:
            shells.append(missing(shell_id, "only on Windows"))
            continue
        path: str | None
        if windows:
            dirs = windows_dirs(env)
            if shell_id == "cmd":
                path = first_existing(
                    [
                        env.get("ComSpec") or env.get("COMSPEC") or "",
                        dirs["system32"] + r"\cmd.exe",
                    ],
                    host.is_file,
                )
            elif shell_id == "powershell":
                path = first_existing(
                    [dirs["system32"] + r"\WindowsPowerShell\v1.0\powershell.exe"], host.is_file
                ) or find_executable("powershell", path_env, windows=True, is_file=host.is_file)
            elif shell_id == "pwsh":
                path = find_executable(
                    "pwsh", path_env, windows=True, pathext=env.get("PATHEXT"), is_file=host.is_file
                ) or first_existing(
                    [
                        dirs["program_files"] + r"\PowerShell\7\pwsh.exe",
                        dirs["program_files"] + r"\PowerShell\7-preview\pwsh.exe",
                    ],
                    host.is_file,
                )
            else:
                path = find_executable(
                    shell_id,
                    path_env,
                    windows=True,
                    pathext=env.get("PATHEXT"),
                    is_file=host.is_file,
                )
                if path is not None and (
                    in_system32(path, env)
                    or (git is not None and path.lower().startswith(git.root.lower() + "\\"))
                ):
                    path = None  # WSL launcher or Git's internal MSYS binaries
        else:
            path = find_executable(shell_id, path_env, windows=False, is_file=host.is_file)
            if path is None and shell_id == "sh" and host.is_file("/bin/sh"):
                path = "/bin/sh"
        shells.append(Shell(shell_id, "builtin", path, True) if path else missing(shell_id))
    return shells


def _custom(definitions: Mapping[str, CustomShell], host: HostInfo) -> list[Shell]:
    result: list[Shell] = []
    windows = host.platform == "win32"
    path_env = host.environ.get("PATH") or host.environ.get("Path")
    for shell_id, definition in definitions.items():
        raw = os.path.expanduser(definition.path)
        if "/" in raw or "\\" in raw:
            path = raw if host.is_file(raw) else None
        else:
            path = find_executable(
                raw,
                path_env,
                windows=windows,
                pathext=host.environ.get("PATHEXT"),
                is_file=host.is_file,
            )
        if path is None:
            result.append(
                Shell(
                    shell_id, "custom", raw, False, reason="executable not found", custom=definition
                )
            )
        else:
            result.append(Shell(shell_id, "custom", path, True, custom=definition))
    return result


# --- registry -----------------------------------------------------------------


@dataclass
class Registry:
    platform: Platform
    shells: dict[str, Shell]
    default: str | None
    warnings: list[str] = field(default_factory=lambda: list[str]())

    def available(self) -> list[Shell]:
        return [shell for shell in self.shells.values() if shell.available]

    def available_ids(self) -> list[str]:
        return [shell.id for shell in self.available()]

    def resolve(self, name: str | None) -> Shell:
        if name is None or not name.strip():
            if self.default is None:
                raise ShellError("no shell is available on this host")
            return self.shells[self.default]
        shell_id = canonical_shell_id(name)
        shell = self.shells.get(shell_id)
        if (
            self.platform == "win32"
            and shell_id in ("bash", "sh")
            and (shell is None or not shell.available)
            and self.shells["git-bash"].available
        ):
            shell = self.shells["git-bash"]
        if shell is None or not shell.available:
            reason = f" ({shell.reason})" if shell is not None and shell.reason else ""
            raise ShellError(
                f"shell '{name}' is not available{reason}. "
                f"Available shells: {', '.join(self.available_ids()) or 'none'}"
            )
        return shell


def select_default(
    platform: Platform,
    shells: Mapping[str, Shell],
    configured: str | None,
    login_shell: str | None,
) -> tuple[str | None, str | None]:
    """Return (default shell id, warning)."""

    def ok(shell_id: str) -> bool:
        return shell_id in shells and shells[shell_id].available

    warning = None
    if configured is not None:
        if ok(configured):
            return configured, None
        warning = (
            f"configured default shell '{configured}' is not available; using the automatic choice"
        )
    if platform == "win32":
        order = ["git-bash", "pwsh", "powershell", "cmd"]
    else:
        login = os.path.basename(login_shell or "")
        order = ([login] if login in BUILTIN_SHELLS else []) + ["bash", "sh"]
    for shell_id in order:
        if ok(shell_id):
            return shell_id, warning
    available = [s.id for s in shells.values() if s.available]
    return (available[0] if available else None), warning


def detect(
    host: HostInfo,
    *,
    custom: Mapping[str, CustomShell] | None = None,
    configured_default: str | None = None,
    probe: ProbeRunner = run_probe,
    budget_s: float = PROBE_BUDGET_S,
) -> Registry:
    """Detect shells, probing versions in parallel within ``budget_s`` in total."""
    started = time.monotonic()
    shells = _candidates(host) + _custom(custom or {}, host)
    to_probe = [s for s in shells if s.available and s.kind == "builtin" and s.path]
    deadline = started + budget_s

    def run_one(shell: Shell) -> tuple[int, str]:
        assert shell.path is not None
        return probe(probe_argv(shell.id, shell.path), max(deadline - time.monotonic(), 0.01))

    if to_probe:
        pool = ThreadPoolExecutor(max_workers=len(to_probe), thread_name_prefix="envelope-probe")
        futures = {pool.submit(run_one, shell): shell for shell in to_probe}
        wait_futures(futures, timeout=max(deadline - time.monotonic(), 0) + 0.5)
        for future, shell in futures.items():
            if not future.done():
                shell.available, shell.reason = False, "probe timed out"
                continue
            try:
                code, out = future.result()
            except TimeoutError:
                shell.available, shell.reason = False, "probe timed out"
                continue
            except OSError as exc:
                shell.available, shell.reason = False, f"probe failed: {exc.strerror or exc}"
                continue
            parse_probe(shell, code, out)
        pool.shutdown(wait=False, cancel_futures=True)

    by_id = {shell.id: shell for shell in shells}
    default, warning = select_default(host.platform, by_id, configured_default, host.login_shell)
    registry = Registry(host.platform, by_id, default, [warning] if warning else [])
    for shell in shells:
        logger.debug(
            "shell %s: %s",
            shell.id,
            f"{shell.path} {shell.version or ''}" if shell.available else shell.reason,
        )
    logger.debug("default shell: %s (detection took %.2fs)", default, time.monotonic() - started)
    return registry


def login_shell_from_env(environ: Mapping[str, str]) -> str | None:
    if sys.platform == "win32":
        return None
    shell = environ.get("SHELL")
    if shell:
        return shell
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_shell
    except (ImportError, KeyError, AttributeError):
        return None


def detect_host(
    custom: Mapping[str, CustomShell] | None = None, configured_default: str | None = None
) -> Registry:
    host = HostInfo(
        platform=current_platform(),
        environ=os.environ,
        login_shell=login_shell_from_env(os.environ),
    )
    return detect(host, custom=custom, configured_default=configured_default)
