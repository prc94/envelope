"""Git for Windows (git-bash) discovery.

Order: registry ``InstallPath`` (HKLM, then HKCU) -> ``git.exe`` found on PATH
-> standard install directories. The launcher used is ``<Git>\\bin\\bash.exe``,
which sets up ``MSYSTEM`` and the MSYS PATH before starting ``usr\\bin\\bash.exe``.
WSL's ``System32\\bash.exe`` is never considered.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from envelope.shells.resolve import find_executable, in_system32, windows_dirs


@dataclass(frozen=True)
class GitBash:
    root: str
    bash: str
    source: str


def _parent(path: str) -> str:
    trimmed = path.rstrip("\\/")
    index = max(trimmed.rfind("\\"), trimmed.rfind("/"))
    return trimmed[:index] if index > 0 else trimmed


def _basename(path: str) -> str:
    trimmed = path.rstrip("\\/")
    return trimmed[max(trimmed.rfind("\\"), trimmed.rfind("/")) + 1 :]


def root_from_git_exe(git_exe: str) -> str | None:
    """``<Git>\\cmd\\git.exe``, ``<Git>\\bin\\git.exe`` or ``<Git>\\mingw64\\bin\\git.exe`` -> ``<Git>``."""
    directory = _parent(git_exe)
    name = _basename(directory).lower()
    if name in ("cmd", "bin"):
        parent = _parent(directory)
        if _basename(parent).lower() in ("mingw64", "mingw32", "clangarm64", "usr"):
            return _parent(parent)
        return parent
    return None


def read_registry_install_paths() -> list[str]:
    if sys.platform != "win32":
        return []
    import winreg

    found: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\GitForWindows", 0, winreg.KEY_READ | view
                ) as key:
                    value, _kind = winreg.QueryValueEx(key, "InstallPath")
            except OSError:
                continue
            if isinstance(value, str) and value and value not in found:
                found.append(value)
    return found


def discover(
    environ: Mapping[str, str],
    *,
    is_file: Callable[[str], bool],
    registry: Callable[[], list[str]] = read_registry_install_paths,
) -> GitBash | None:
    candidates: list[tuple[str, str]] = [(path, "registry") for path in registry()]
    git_exe = find_executable(
        "git.exe", environ.get("PATH") or environ.get("Path"), windows=True, is_file=is_file
    )
    if git_exe is not None and (root := root_from_git_exe(git_exe)) is not None:
        candidates.append((root, "PATH"))
    dirs = windows_dirs(environ)
    for base in (dirs["program_files"], dirs["program_files_x86"]):
        candidates.append((base.rstrip("\\") + r"\Git", "standard directory"))
    if dirs["local_app_data"]:
        candidates.append(
            (dirs["local_app_data"].rstrip("\\") + r"\Programs\Git", "standard directory")
        )
    candidates.append((r"C:\Program Files\Git", "standard directory"))

    for root, source in candidates:
        root = root.rstrip("\\/")
        bash = root + r"\bin\bash.exe"
        if in_system32(bash, environ):
            continue
        if is_file(bash):
            return GitBash(root=root, bash=bash, source=source)
    return None


def msys_path_dirs(root: str) -> list[str]:
    """Directories git-bash puts on PATH (for cross-shell command lookup)."""
    return [
        root + r"\usr\local\bin",
        root + r"\mingw64\bin",
        root + r"\usr\bin",
        root + r"\bin",
        root + r"\cmd",
    ]
