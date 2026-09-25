"""Locate real executables on PATH without spawning anything.

Batch wrappers (``.bat``/``.cmd``, e.g. npm-style ``pwsh.cmd`` shims) are never
returned: launching through them re-parses arguments through cmd.exe and breaks
quoting and process control.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence

REJECTED_EXTENSIONS = frozenset({".bat", ".cmd"})
WINDOWS_EXEC_EXTENSIONS = (".exe", ".com")


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_executable(
    name: str,
    path_env: str | None,
    *,
    windows: bool,
    pathext: str | None = None,
    is_file: Callable[[str], bool] | None = None,
) -> str | None:
    """Return the first real executable called ``name`` on ``path_env``, or None.

    On Windows only ``.exe``/``.com`` are considered (filtered from PATHEXT), so a
    ``pwsh.cmd`` shim is skipped even when it comes first on PATH.
    """
    if not path_env:
        return None
    check = is_file or (_is_file if windows else _is_executable)
    separator = ";" if windows else ":"
    if windows:
        root, ext = os.path.splitext(name)
        if ext.lower() in REJECTED_EXTENSIONS:
            return None
        allowed = [
            e.lower()
            for e in (pathext or ".COM;.EXE").split(";")
            if e.lower() in WINDOWS_EXEC_EXTENSIONS
        ] or list(WINDOWS_EXEC_EXTENSIONS)
        names = [name] if ext.lower() in WINDOWS_EXEC_EXTENSIONS else [root + e for e in allowed]
    else:
        names = [name]
    for directory in path_env.split(separator):
        directory = directory.strip().strip('"')
        if not directory:
            continue
        for candidate_name in names:
            candidate = (
                f"{directory.rstrip(chr(92) + '/')}\\{candidate_name}"
                if windows
                else os.path.join(directory, candidate_name)
            )
            if check(candidate):
                return candidate
    return None


def first_existing(
    candidates: Sequence[str], is_file: Callable[[str], bool] | None = None
) -> str | None:
    check = is_file or _is_file
    for candidate in candidates:
        if candidate and check(candidate):
            return candidate
    return None


def windows_dirs(environ: Mapping[str, str]) -> dict[str, str]:
    """Well-known Windows directories with sensible fallbacks."""
    system_root = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
    return {
        "system_root": system_root,
        "system32": system_root.rstrip("\\") + r"\System32",
        "program_files": environ.get("ProgramFiles")
        or environ.get("PROGRAMFILES")
        or r"C:\Program Files",
        "program_files_x86": environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        "local_app_data": environ.get("LOCALAPPDATA") or "",
    }


def in_system32(path: str, environ: Mapping[str, str]) -> bool:
    system32 = windows_dirs(environ)["system32"].lower().rstrip("\\") + "\\"
    normalized = path.replace("/", "\\").lower()
    return normalized.startswith(system32) or "\\windows\\system32\\" in normalized
