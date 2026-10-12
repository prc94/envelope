#!/usr/bin/env python3
"""Containment-unit launcher for one harness run. Standard library only.

    python launch.py SPEC.json

SPEC is a JSON object:
    argv      list of strings: the harness command
    cwd       working directory
    env       optional mapping added to (overriding) this process's environment
    timeout   seconds the harness may run
    stdout    file receiving the harness's stdout (the transcript)
    stderr    optional file for stderr (default: the same file as stdout)
    result    file to which the result JSON is written (also printed as the last line)
    grace     seconds to wait for the unit to empty after it is terminated (default 5)

The harness and everything it starts run in one unit: a Windows Job Object (created with
kill-on-close, the harness started suspended, assigned, then resumed, so no descendant can
escape) or, on POSIX (used only to test this logic), a session. When the harness exits, the
processes still alive in the unit (other than Windows console hosts) are its leftovers: they are
listed, then killed. On timeout
the whole unit is killed. The launcher itself and the runner's other processes (tunnels,
collect, snapshots) are not in the unit.

Result: {exit_code, timed_out, leftovers: [{pid, name}], at_timeout: [...], emptied, duration,
error}. `emptied` is whether the unit had no processes left after termination.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import IO, Any

Member = dict[str, Any]

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    KILL_ON_JOB_CLOSE = 0x00002000
    EXTENDED_LIMIT_CLASS = 9
    ACCOUNTING_CLASS = 1
    PID_LIST_CLASS = 3
    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PIDS = 1024

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("Basic", _BasicLimit),
            ("Io", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _PidList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * MAX_PIDS),
        ]

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _nt.NtResumeProcess.restype = ctypes.c_long

    def _fail(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(0, f"{what} failed: {ctypes.FormatError(code)}", None, code)

    def process_names() -> dict[int, str]:
        """pid -> executable name for every process, via Toolhelp32 (WMI, tasklist and taskkill
        are denied to a standard user over SSH)."""
        snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        names: dict[int, str] = {}
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            names[int(entry.th32ProcessID)] = entry.szExeFile
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
        _k32.CloseHandle(snapshot)
        return names

    class Unit:
        """A kill-on-close Job Object. Nested jobs work inside the OpenSSH session job (S2)."""

        def __init__(self) -> None:
            self.job = _k32.CreateJobObjectW(None, None)
            if not self.job:
                raise _fail("CreateJobObjectW")
            info = _ExtendedLimit()
            info.Basic.LimitFlags = KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(
                self.job, EXTENDED_LIMIT_CLASS, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise _fail("SetInformationJobObject")

        def start(
            self,
            argv: list[str],
            env: dict[str, str],
            cwd: str,
            stdout: IO[bytes],
            stderr: IO[bytes],
            stdin: IO[bytes] | int = subprocess.DEVNULL,
        ) -> subprocess.Popen[bytes]:
            proc = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
                creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            )
            handle = int(proc._handle)  # type: ignore[attr-defined]
            try:
                if not _k32.AssignProcessToJobObject(self.job, handle):
                    raise _fail("AssignProcessToJobObject")
                status = _nt.NtResumeProcess(handle)
                if status != 0:
                    raise OSError(
                        0, f"NtResumeProcess failed: NTSTATUS 0x{status & 0xFFFFFFFF:08X}"
                    )
            except OSError:
                proc.kill()
                raise
            return proc

        def members(self) -> list[Member]:
            info = _PidList()
            if not _k32.QueryInformationJobObject(
                self.job, PID_LIST_CLASS, ctypes.byref(info), ctypes.sizeof(info), None
            ):
                raise _fail("QueryInformationJobObject")
            pids = [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]
            names = process_names() if pids else {}
            return [{"pid": pid, "name": names.get(pid, "")} for pid in pids]

        def terminate(self) -> None:
            _k32.TerminateJobObject(self.job, 1)

        def close(self) -> None:
            _k32.CloseHandle(self.job)

else:

    class Unit:  # type: ignore[no-redef]
        """POSIX stand-in (a session), so the launcher's logic can be tested off Windows."""

        def __init__(self) -> None:
            self.sid: int | None = None

        def start(
            self,
            argv: list[str],
            env: dict[str, str],
            cwd: str,
            stdout: IO[bytes],
            stderr: IO[bytes],
            stdin: IO[bytes] | int = subprocess.DEVNULL,
        ) -> subprocess.Popen[bytes]:
            proc = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            self.sid = proc.pid
            return proc

        def members(self) -> list[Member]:
            found: list[Member] = []
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                        stat = fh.read()
                except OSError:
                    continue
                name = stat[stat.index("(") + 1 : stat.rindex(")")]
                fields = stat[stat.rindex(")") + 2 :].split()
                if int(fields[3]) == self.sid and fields[0] != "Z":  # session id; not a zombie
                    found.append({"pid": int(entry), "name": name})
            return found

        def terminate(self) -> None:
            for member in self.members():
                try:
                    os.kill(member["pid"], 9)
                except OSError:
                    pass

        def close(self) -> None:
            pass


# Windows attaches a console host to a console process; it is a job member for a moment after
# the harness exits and is not something the agent started.
CONSOLE_HOSTS = {"conhost.exe", "openconsole.exe"}


def find_leftovers(unit: Unit, harness_pid: int) -> list[Member]:
    """Members other than the harness and console hosts. A member whose name cannot be found
    exited between the job query and the process snapshot."""
    return [
        m
        for m in unit.members()
        if m["pid"] != harness_pid and m["name"] and m["name"].lower() not in CONSOLE_HOSTS
    ]


def wait_empty(unit: Unit, grace: float) -> bool:
    deadline = time.monotonic() + grace
    while True:
        if not unit.members():
            return True
        if time.monotonic() >= deadline:
            return False
        unit.terminate()
        time.sleep(0.05)


def run(spec: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exit_code": None,
        "timed_out": False,
        "leftovers": [],
        "at_timeout": [],
        "emptied": False,
        "duration": 0.0,
        "error": None,
    }
    started = time.monotonic()
    env = {**os.environ, **spec.get("env", {})}
    argv = list(spec["argv"])
    argv[0] = shutil.which(argv[0], path=env.get("PATH")) or argv[0]  # PATHEXT: qwen -> qwen.CMD
    unit = Unit()
    try:
        with open(spec.get("stdin") or os.devnull, "rb") as inp, open(spec["stdout"], "wb") as out:
            if spec.get("stderr"):
                with open(spec["stderr"], "wb") as err:
                    proc = unit.start(argv, env, spec["cwd"], out, err, inp)
            else:
                proc = unit.start(argv, env, spec["cwd"], out, out, inp)
        try:
            result["exit_code"] = proc.wait(timeout=spec["timeout"])
            result["leftovers"] = find_leftovers(unit, proc.pid)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            result["at_timeout"] = unit.members()
        unit.terminate()
        result["emptied"] = wait_empty(unit, float(spec.get("grace", 5)))
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        unit.close()
    result["duration"] = time.monotonic() - started
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    result = run(spec)
    text = json.dumps(result)
    with open(spec["result"], "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
