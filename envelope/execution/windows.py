"""Windows backend: Job Objects for per-run tree kill and crash-safe cleanup.

Design D7 (pending verification by spike S2 on a Windows host):

* At startup envelope assigns itself to a job with KILL_ON_JOB_CLOSE, so if
  envelope dies for any reason the OS kills every process it started.
* Each run gets its own nested job (also KILL_ON_JOB_CLOSE) so a single
  command's tree can be terminated with TerminateJobObject.
* Children are created with CREATE_SUSPENDED, assigned to the run job and then
  resumed with NtResumeProcess (subprocess closes the primary thread handle),
  so grandchildren cannot be spawned before assignment.
* CREATE_NO_WINDOW avoids console flashes; CREATE_NEW_PROCESS_GROUP enables
  CTRL_BREAK_EVENT for a later graceful stop.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from functools import cache
from importlib import resources
from typing import IO

from envelope.execution.backend import ShellExit, SpawnError, spawn_error_from_os
from envelope.log import logger

DETACHED_PROCESS = 0x00000008
CTRL_BREAK_HELPER_TIMEOUT_S = 3.0


@cache
def ctrlbreak_source() -> str:
    return (
        resources.files("envelope.execution").joinpath("_ctrlbreak.py").read_text(encoding="utf-8")
    )


CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS = 3
_MAX_JOB_PIDS = 1024
TCP_TABLE_OWNER_PID_LISTENER = 3
AF_INET = 2
AF_INET6 = 23
ERROR_INSUFFICIENT_BUFFER = 122

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

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
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
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

    class _ProcessIdList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _MAX_JOB_PIDS),
        ]

    class _TcpRowOwnerPid(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    class _Tcp6RowOwnerPid(ctypes.Structure):
        _fields_ = [
            ("ucLocalAddr", ctypes.c_ubyte * 16),
            ("dwLocalScopeId", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("ucRemoteAddr", ctypes.c_ubyte * 16),
            ("dwRemoteScopeId", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwState", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    _iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _iphlpapi.GetExtendedTcpTable.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    _iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _error(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(0, f"{what} failed: {ctypes.FormatError(code)}", None, code)

    def create_kill_on_close_job() -> int:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _error("CreateJobObjectW")
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = _error("SetInformationJobObject")
            _kernel32.CloseHandle(job)
            raise error
        return int(job)

    def assign(job: int, process_handle: int) -> None:
        if not _kernel32.AssignProcessToJobObject(job, process_handle):
            raise _error("AssignProcessToJobObject")

    def resume(process_handle: int) -> None:
        status = _ntdll.NtResumeProcess(process_handle)
        if status != 0:
            raise OSError(0, f"NtResumeProcess failed: NTSTATUS 0x{status & 0xFFFFFFFF:08X}")

    def terminate_job(job: int) -> None:
        _kernel32.TerminateJobObject(job, 1)

    def active_processes(job: int) -> int:
        info = _Accounting()
        if not _kernel32.QueryInformationJobObject(
            job,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            return -1
        return int(info.ActiveProcesses)

    def close_handle(handle: int) -> None:
        _kernel32.CloseHandle(handle)

    def job_pids(job: int) -> frozenset[int] | None:
        """Every PID in ``job`` (design B5/W3: filters the port table by the
        run's job membership rather than a parent/child walk, since the
        owning process can be a grandchild of the launched shell)."""
        info = _ProcessIdList()
        if not _kernel32.QueryInformationJobObject(
            job,
            JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            return None
        count = min(info.NumberOfProcessIdsInList, _MAX_JOB_PIDS)
        return frozenset(int(info.ProcessIdList[i]) for i in range(count))

    def _tcp_listener_ports(pids: frozenset[int], af: int) -> set[int]:
        """LISTEN rows owned by ``pids`` (design B5/W3: `GetExtendedTcpTable`,
        confirmed to work as an unprivileged standard user and to report the
        job's own grandchild processes, not just the launched shell)."""
        size = wintypes.DWORD(0)
        row_type = _TcpRowOwnerPid if af == AF_INET else _Tcp6RowOwnerPid
        result = _iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result != ERROR_INSUFFICIENT_BUFFER:
            return set()
        buf = ctypes.create_string_buffer(size.value)
        result = _iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result != 0:
            return set()
        num_entries = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = (row_type * num_entries).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
        ports: set[int] = set()
        for row in rows:
            if row.dwOwningPid in pids:
                # dwLocalPort holds the port in network byte order in its low word.
                ports.add(((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF))
        return ports

    def listening_ports_in_pids(pids: frozenset[int]) -> frozenset[int]:
        return frozenset(_tcp_listener_ports(pids, AF_INET) | _tcp_listener_ports(pids, AF_INET6))

    def current_process() -> int:
        return int(_kernel32.GetCurrentProcess())


_self_job: int | None = None
self_job_error: str | None = None


def self_job_established() -> bool:
    return _self_job is not None


def ensure_self_job() -> bool:
    """Put envelope in a kill-on-close job (crash safety). Returns True on success."""
    global _self_job, self_job_error
    if sys.platform != "win32":
        return False
    if _self_job is not None:
        return True
    try:
        job = create_kill_on_close_job()
        assign(job, current_process())
    except OSError as exc:
        self_job_error = str(exc)
        logger.warning(
            "could not place envelope in a kill-on-close job (%s); child processes may "
            "outlive envelope if it crashes",
            exc,
        )
        return False
    _self_job = job  # intentionally never closed: closing it would kill our children
    return True


def send_ctrl_break(candidates: list[int]) -> bool:
    """Design B7/spike E2: best-effort graceful-stop request via the
    ctrl-break helper -- ``candidates[0]`` is the run's process-group id
    (the actual CTRL_BREAK_EVENT target), the rest are other live job
    members to attach through if it has already exited. Returns whether the
    event was actually delivered (attached to a console and sent), not
    whether the target honours it -- the caller still waits out its own
    grace period and forces the kill if the tree does not exit."""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                ctrlbreak_source(),
                *map(str, candidates),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS,
        )
        return proc.wait(CTRL_BREAK_HELPER_TIMEOUT_S) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class WindowsProcess:
    def __init__(self, popen: subprocess.Popen[bytes], job: int) -> None:
        self._popen = popen
        self._job = job
        assert popen.stdout is not None and popen.stderr is not None
        self.stdout: IO[bytes] = popen.stdout
        self.stderr: IO[bytes] = popen.stderr
        self.stdin: IO[bytes] | None = popen.stdin
        self._lock = threading.Lock()
        self._closed = False

    def wait_shell(self) -> ShellExit:
        return ShellExit(self._popen.wait())

    def terminate_tree(self) -> None:
        if sys.platform == "win32":
            with self._lock:
                if not self._closed:
                    terminate_job(self._job)

    def wait_terminated(self, timeout: float) -> bool:
        if sys.platform != "win32":
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._closed:
                    return True
                active = active_processes(self._job)
            if active == 0:
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        for stream in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if sys.platform == "win32":
            with self._lock:
                if not self._closed:
                    self._closed = True
                    close_handle(self._job)  # kill-on-close reaps leftovers

    def tree_pids(self) -> frozenset[int] | None:
        if sys.platform != "win32":
            return None
        with self._lock:
            if self._closed:
                return frozenset()
            return job_pids(self._job)

    def request_graceful_stop(self) -> bool:
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._closed:
                return False
            root = self._popen.pid
            members = job_pids(self._job)
        candidates = [root, *[p for p in (members or ()) if p != root]]
        return send_ctrl_break(candidates)


class WindowsBackend:
    def __init__(self) -> None:
        self.self_job = ensure_self_job()

    def spawn(
        self,
        *,
        shell: str,
        args: list[str] | str,
        cwd: str,
        env: dict[str, str],
        stdin: bool,
    ) -> WindowsProcess:
        if sys.platform != "win32":
            raise RuntimeError("WindowsBackend requires Windows")
        path = args[0] if isinstance(args, list) else shell
        try:
            job = create_kill_on_close_job()
        except OSError as exc:
            raise spawn_error_from_os(shell, path, exc) from None
        try:
            popen = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            )
        except OSError as exc:
            close_handle(job)
            raise spawn_error_from_os(shell, path, exc) from None
        handle = int(popen._handle)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
        try:
            assign(job, handle)
            resume(handle)
        except OSError as exc:
            popen.kill()
            close_handle(job)
            raise SpawnError(shell, path, getattr(exc, "winerror", None), str(exc)) from None
        return WindowsProcess(popen, job)

    def shutdown(self) -> None:
        # Per-run jobs are closed by their runs; the self job kills the rest at exit.
        return
