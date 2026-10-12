# pyright: basic, reportAttributeAccessIssue=false
"""Check whether a Windows spool owner process is still running."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


def owner_alive(pid: int) -> bool:
    """Use the process object's signaled state; os.kill(pid, 0) is unreliable here."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() not in (87, 1168)  # invalid PID / not found
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0 means exited
    finally:
        kernel.CloseHandle(handle)
