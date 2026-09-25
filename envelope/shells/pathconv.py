"""In-process path translation between Windows, forward-slash and MSYS forms.

Never shells out to ``cygpath``. Handles drive letters, UNC paths and spaces.
"""

from __future__ import annotations

import re

_DRIVE = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$", re.DOTALL)
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(?:/(.*))?$", re.DOTALL)
_MSYS_CYGDRIVE = re.compile(r"^/cygdrive/([A-Za-z])(?:/(.*))?$", re.DOTALL)


def is_msys_path(path: str) -> bool:
    return bool(_MSYS_DRIVE.match(path) or _MSYS_CYGDRIVE.match(path))


def to_windows(path: str) -> str:
    """``/c/Users/x``, ``C:/Users/x``, ``//server/share`` -> ``C:\\Users\\x``, ``\\\\server\\share``.

    Paths that are neither drive-based nor UNC (e.g. relative paths or MSYS
    paths like ``/usr/bin``) are returned with slashes converted only when they
    are relative; absolute MSYS-internal paths are returned unchanged.
    """
    if match := (_MSYS_CYGDRIVE.match(path) or _MSYS_DRIVE.match(path)):
        drive, rest = match.group(1), match.group(2) or ""
        return f"{drive.upper()}:\\" + rest.replace("/", "\\").rstrip("\\")
    if match := _DRIVE.match(path):
        drive, rest = match.group(1), match.group(2) or ""
        return f"{drive.upper()}:\\" + rest.replace("/", "\\").rstrip("\\")
    if path.startswith(("//", "\\\\")):
        return "\\\\" + path[2:].replace("/", "\\").rstrip("\\")
    if path.startswith("/"):
        return path
    return path.replace("/", "\\")


def to_msys(path: str) -> str:
    """``C:\\Users\\x`` or ``C:/Users/x`` -> ``/c/Users/x``; UNC -> ``//server/share``."""
    if _MSYS_DRIVE.match(path) or _MSYS_CYGDRIVE.match(path):
        return path
    if match := _DRIVE.match(path):
        drive, rest = match.group(1), match.group(2) or ""
        rest = rest.replace("\\", "/").rstrip("/")
        return f"/{drive.lower()}" + (f"/{rest}" if rest else "")
    if path.startswith(("\\\\", "//")):
        return "//" + path[2:].replace("\\", "/").rstrip("/")
    return path.replace("\\", "/")


def to_forward(path: str) -> str:
    """Windows path with forward slashes: ``C:\\Users\\x`` -> ``C:/Users/x``."""
    return to_windows(path).replace("\\", "/")
