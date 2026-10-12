# pyright: basic, reportAttributeAccessIssue=false
"""Read the cursor row of another process's hidden Windows console.

Run in a detached helper: AttachConsole is process-wide and must never affect
envelope's own console. The target PID is a member of one run's Job Object.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SmallRect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_short) for name in ("Left", "Top", "Right", "Bottom")]


class ScreenInfo(ctypes.Structure):
    _fields_ = [
        ("dwSize", Coord),
        ("dwCursorPosition", Coord),
        ("wAttributes", wintypes.WORD),
        ("srWindow", SmallRect),
        ("dwMaximumWindowSize", Coord),
    ]


def main(pid: int) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ScreenInfo)]
    kernel.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
    kernel.ReadConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        Coord,
        wintypes.LPDWORD,
    ]
    kernel.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
    if not kernel.AttachConsole(pid):
        return 1
    handle = kernel.CreateFileW("CONOUT$", 0x80000000, 3, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        return 1
    try:
        info = ScreenInfo()
        if not kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return 1
        width = max(0, min(int(info.dwSize.X), 4096))
        if width == 0:
            return 1
        buffer = ctypes.create_unicode_buffer(width)
        count = wintypes.DWORD()
        if not kernel.ReadConsoleOutputCharacterW(
            handle, buffer, width, Coord(0, info.dwCursorPosition.Y), ctypes.byref(count)
        ):
            return 1
        sys.stdout.buffer.write("".join(buffer[: count.value]).rstrip().encode("utf-8"))
        return 0
    finally:
        kernel.CloseHandle(handle)
        kernel.FreeConsole()


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1])))
