from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from envelope.config.settings import CustomShell
from envelope.shells import gitbash
from envelope.shells.pathconv import to_forward, to_msys, to_windows
from envelope.shells.registry import (
    HostInfo,
    Shell,
    ShellError,
    detect,
    select_default,
)
from envelope.shells.resolve import find_executable

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX host detection")

WIN_ENV = {
    "SystemRoot": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD;.VBS",
    "ComSpec": r"C:\Windows\System32\cmd.exe",
}


def fake_fs(*files: str):
    lowered = {f.lower() for f in files}
    return lambda path: path.lower() in lowered


def fake_probe(outputs: dict[str, str]):
    def probe(argv: Sequence[str], timeout: float) -> tuple[int, str]:
        name = argv[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
        return 0, outputs.get(name, "")

    return probe


WIN_PROBES = {
    "bash.exe": "GNU bash, version 5.2.37(1)-release (x86_64-pc-msys)",
    "pwsh.exe": "7.4.6|ConstrainedLanguage|RemoteSigned",
    "powershell.exe": "5.1.22621.4391|ConstrainedLanguage|AllSigned",
    "cmd.exe": "\nMicrosoft Windows [Version 10.0.22631.4460]",
}


# --- executable resolution ------------------------------------------------------


def test_cmd_shim_is_not_a_shell() -> None:
    is_file = fake_fs(r"C:\npm\pwsh.cmd")
    assert (
        find_executable(
            "pwsh", r"C:\npm", windows=True, pathext=WIN_ENV["PATHEXT"], is_file=is_file
        )
        is None
    )
    assert find_executable("pwsh.cmd", r"C:\npm", windows=True, is_file=is_file) is None


def test_real_exe_found_after_shim() -> None:
    is_file = fake_fs(r"C:\npm\pwsh.cmd", r"C:\ps\pwsh.exe")
    found = find_executable(
        "pwsh", r"C:\npm;C:\ps", windows=True, pathext=WIN_ENV["PATHEXT"], is_file=is_file
    )
    assert found == r"C:\ps\pwsh.exe"


def test_windows_registry_ignores_pwsh_shim() -> None:
    env = {**WIN_ENV, "PATH": r"C:\npm"}
    host = HostInfo("win32", env, is_file=fake_fs(r"C:\npm\pwsh.cmd"), registry=list)
    registry = detect(host, probe=fake_probe(WIN_PROBES))
    assert not registry.shells["pwsh"].available


# --- git-bash discovery -----------------------------------------------------------


def test_git_default_location() -> None:
    is_file = fake_fs(r"C:\Program Files\Git\bin\bash.exe")
    found = gitbash.discover(WIN_ENV, is_file=is_file, registry=list)
    assert found is not None
    assert found.bash == r"C:\Program Files\Git\bin\bash.exe"


def test_git_from_registry_first() -> None:
    is_file = fake_fs(r"D:\Tools\Git\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe")
    found = gitbash.discover(WIN_ENV, is_file=is_file, registry=lambda: [r"D:\Tools\Git"])
    assert found is not None and found.bash == r"D:\Tools\Git\bin\bash.exe"
    assert found.source == "registry"


@pytest.mark.parametrize(
    "git_exe",
    [
        r"E:\PortableGit\cmd\git.exe",
        r"E:\PortableGit\mingw64\bin\git.exe",
        r"E:\PortableGit\bin\git.exe",
    ],
)
def test_git_from_path(git_exe: str) -> None:
    directory = git_exe.rsplit("\\", 1)[0]
    is_file = fake_fs(git_exe, r"E:\PortableGit\bin\bash.exe")
    found = gitbash.discover({**WIN_ENV, "PATH": directory}, is_file=is_file, registry=list)
    assert found is not None and found.root == r"E:\PortableGit" and found.source == "PATH"


def test_wsl_bash_is_not_git_bash() -> None:
    env = {**WIN_ENV, "PATH": r"C:\Windows\System32"}
    host = HostInfo(
        "win32",
        env,
        is_file=fake_fs(r"C:\Windows\System32\bash.exe", r"C:\Windows\System32\cmd.exe"),
        registry=list,
    )
    registry = detect(host, probe=fake_probe(WIN_PROBES))
    assert not registry.shells["git-bash"].available
    assert not registry.shells["bash"].available
    assert registry.default == "cmd"
    with pytest.raises(ShellError):
        registry.resolve("bash")


# --- default selection and aliases ---------------------------------------------------


def win_host(*extra: str) -> HostInfo:
    files = [
        r"C:\Windows\System32\cmd.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        *extra,
    ]
    return HostInfo("win32", {**WIN_ENV, "PATH": ""}, is_file=fake_fs(*files), registry=list)


def test_windows_without_git_defaults_to_pwsh() -> None:
    registry = detect(win_host(), probe=fake_probe(WIN_PROBES))
    assert registry.default == "pwsh"
    pwsh = registry.shells["pwsh"]
    assert pwsh.version == "7.4.6" and pwsh.language_mode == "ConstrainedLanguage"
    assert registry.shells["cmd"].version == "10.0.22631.4460"


def test_windows_with_git_defaults_to_git_bash_and_aliases() -> None:
    registry = detect(win_host(r"C:\Program Files\Git\bin\bash.exe"), probe=fake_probe(WIN_PROBES))
    assert registry.default == "git-bash"
    assert registry.resolve("bash").id == "git-bash"
    assert registry.resolve("GitBash").id == "git-bash"
    assert registry.resolve("git_bash").id == "git-bash"
    assert registry.resolve("PowerShell7").id == "pwsh"
    assert registry.resolve("pwsh7").id == "pwsh"
    assert registry.resolve(None).id == "git-bash"
    with pytest.raises(ShellError, match="Available shells: git-bash, pwsh, powershell, cmd"):
        registry.resolve("tcsh")


def test_configured_default() -> None:
    registry = detect(win_host(), configured_default="cmd", probe=fake_probe(WIN_PROBES))
    assert registry.default == "cmd"
    assert registry.resolve(None).id == "cmd"


def test_unavailable_configured_default_falls_back_with_warning() -> None:
    shells = {"bash": Shell("bash", "builtin", "/bin/bash", True)}
    default, warning = select_default("linux", shells, "zsh", None)
    assert default == "bash" and warning is not None and "zsh" in warning


def test_posix_default_prefers_supported_login_shell() -> None:
    shells = {
        "bash": Shell("bash", "builtin", "/bin/bash", True),
        "zsh": Shell("zsh", "builtin", "/bin/zsh", True),
        "sh": Shell("sh", "builtin", "/bin/sh", True),
    }
    assert select_default("linux", shells, None, "/usr/bin/zsh")[0] == "zsh"
    assert select_default("darwin", shells, None, "/usr/local/bin/tcsh")[0] == "bash"
    del shells["bash"]
    assert select_default("linux", shells, None, None)[0] == "sh"


# --- detection on the real host ---------------------------------------------------------


@posix_only
def test_detects_host_shells() -> None:
    host = HostInfo("linux" if sys.platform != "darwin" else "darwin", os.environ)
    registry = detect(host)
    bash = registry.shells["bash"]
    assert bash.available and bash.path and bash.version
    assert registry.shells["sh"].available
    assert not registry.shells["cmd"].available
    assert registry.default in ("bash", "zsh", "sh", "fish")


@posix_only
def test_hanging_probe_times_out(tmp_path: Path) -> None:
    fake = tmp_path / "zsh"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    started = time.monotonic()
    registry = detect(HostInfo("linux", env), budget_s=1.0)
    elapsed = time.monotonic() - started
    zsh = registry.shells["zsh"]
    assert not zsh.available and zsh.reason == "probe timed out"
    assert registry.shells["bash"].available
    assert elapsed < 3


def test_custom_shell_registered(tmp_path: Path) -> None:
    exe = tmp_path / "nu"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    custom = {"nu": CustomShell("nu", str(exe), ("-c", "{script}"))}
    registry = detect(HostInfo("linux", {"PATH": ""}), custom=custom, probe=fake_probe({}))
    assert registry.resolve("NU").id == "nu"
    assert registry.shells["nu"].kind == "custom"
    missing = {"xs": CustomShell("xs", "/nope/xs", ("{script}",))}
    registry = detect(HostInfo("linux", {"PATH": ""}), custom=missing, probe=fake_probe({}))
    assert registry.shells["xs"].reason == "executable not found"


# --- path translation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "windows", "msys", "forward"),
    [
        (r"C:\src\app", r"C:\src\app", "/c/src/app", "C:/src/app"),
        ("C:/src/app", r"C:\src\app", "/c/src/app", "C:/src/app"),
        ("/c/src/app", r"C:\src\app", "/c/src/app", "C:/src/app"),
        ("/cygdrive/d/x y/z", r"D:\x y\z", "/cygdrive/d/x y/z", "D:/x y/z"),
        (
            r"c:\Program Files\Git",
            r"C:\Program Files\Git",
            "/c/Program Files/Git",
            "C:/Program Files/Git",
        ),
        ("C:\\", "C:\\", "/c", "C:/"),
        ("/d", "D:\\", "/d", "D:/"),
        (r"\\server\share\dir", r"\\server\share\dir", "//server/share/dir", "//server/share/dir"),
        ("//server/share/a b", r"\\server\share\a b", "//server/share/a b", "//server/share/a b"),
    ],
)
def test_path_translation(given: str, windows: str, msys: str, forward: str) -> None:
    assert to_windows(given) == windows
    assert to_msys(given) == msys
    assert to_forward(given) == forward
    assert to_windows(to_msys(given)) == windows
