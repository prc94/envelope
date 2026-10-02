"""Platform-neutral process interface implemented by the POSIX and Windows backends."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import IO, Protocol

# Windows error codes for application-control blocks.
ERROR_ACCESS_DISABLED_BY_POLICY = 1260
ERROR_APPLICATION_CONTROL_BLOCKED = 4551
POLICY_BLOCK_CODES = frozenset({ERROR_ACCESS_DISABLED_BY_POLICY, ERROR_APPLICATION_CONTROL_BLOCKED})


@dataclass
class SpawnError(Exception):
    shell: str
    path: str
    code: int | None
    message: str

    @property
    def blocked_by_policy(self) -> bool:
        return self.code in POLICY_BLOCK_CODES

    def describe(self) -> str:
        code = f" (error {self.code})" if self.code is not None else ""
        text = f"could not start shell '{self.shell}' at {self.path}: {self.message}{code}"
        if self.blocked_by_policy:
            text += (
                ". The shell executable is blocked by group policy / application control "
                "(AppLocker or WDAC); ask an administrator to allow it or choose another shell"
            )
        return text


def spawn_error_from_os(shell: str, path: str, exc: OSError) -> SpawnError:
    code = getattr(exc, "winerror", None) if sys.platform == "win32" else None
    if code is None:
        code = exc.errno
    return SpawnError(shell, path, code, exc.strerror or str(exc))


@dataclass
class ShellExit:
    exit_code: int | None
    signal: int | None = None


class Process(Protocol):
    """A started command. Blocking methods are called from worker threads."""

    stdout: IO[bytes]
    stderr: IO[bytes]
    stdin: IO[bytes] | None

    def wait_shell(self) -> ShellExit:
        """Block until the shell process itself has exited."""
        ...

    def terminate_tree(self) -> None:
        """Kill every process of this command (idempotent)."""
        ...

    def wait_terminated(self, timeout: float) -> bool:
        """Block until the whole tree is gone; True if confirmed."""
        ...

    def close(self) -> None: ...

    def tree_pids(self) -> frozenset[int] | None:
        """Every live PID of this command's tree, for `port:` waits (design
        B5); `None` where the platform offers no way to enumerate it (called
        from the event loop, so this must not block)."""
        ...

    def request_graceful_stop(self) -> bool:
        """Ask the tree to terminate gracefully where the platform can
        deliver such a request (SIGTERM on POSIX; CTRL+BREAK on Windows
        where deliverable through envelope's launch chain -- design B7).
        Returns whether a request was actually sent, not whether the tree
        honoured it; callers still must fall back to ``terminate_tree()``
        after a grace period."""
        ...


class Backend(Protocol):
    def spawn(
        self,
        *,
        shell: str,
        args: list[str] | str,
        cwd: str,
        env: dict[str, str],
        stdin: bool,
    ) -> Process:
        """Start the command; raise SpawnError when the shell cannot be started."""
        ...

    def shutdown(self) -> None:
        """Called once at exit; kill anything still running."""
        ...


def default_backend() -> Backend:
    if sys.platform == "win32":
        from envelope.execution.windows import WindowsBackend

        return WindowsBackend()
    from envelope.execution.posix import PosixBackend

    return PosixBackend()
