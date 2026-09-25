"""POSIX backend: every command runs under the launch shim (see ``_shim.py``).

Recipe (spike S3): envelope starts the shim in a new session; the shim is a
child subreaper with PDEATHSIG (Linux) or a kqueue parent watch (macOS),
execs the shell, reports its status on a pipe, and on SIGTERM kills every
descendant.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from functools import cache
from importlib import resources
from typing import IO

from envelope.execution.backend import ShellExit, SpawnError, spawn_error_from_os
from envelope.log import logger

KILL_GRACE_S = 3.0


@cache
def shim_source() -> str:
    return resources.files("envelope.execution").joinpath("_shim.py").read_text(encoding="utf-8")


class PosixProcess:
    def __init__(
        self, popen: subprocess.Popen[bytes], status_fd: int, shell: str, path: str
    ) -> None:
        self._popen = popen
        assert popen.stdout is not None and popen.stderr is not None
        self.stdout: IO[bytes] = popen.stdout
        self.stderr: IO[bytes] = popen.stderr
        self.stdin: IO[bytes] | None = popen.stdin
        self._status_fd = status_fd
        self._shell = shell
        self._path = path
        self._lock = threading.Lock()
        self._exit: ShellExit | None = None
        self._exit_event = threading.Event()
        self._started = threading.Event()
        self._error: SpawnError | None = None
        self.shell_pid: int | None = None
        self._killed = False
        threading.Thread(target=self._read_status, name="envelope-status", daemon=True).start()

    def _read_status(self) -> None:
        pending = bytearray()
        try:
            while True:
                chunk: bytes = os.read(self._status_fd, 4096)
                if not chunk:
                    break
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    line = bytes(pending[:end])
                    del pending[: end + 1]
                    self._on_status(line.decode(errors="replace"))
        except OSError:
            pass
        finally:
            os.close(self._status_fd)
            self._started.set()
            self._exit_event.set()

    def _on_status(self, line: str) -> None:
        kind, _, rest = line.partition(" ")
        if kind == "pid":
            self.shell_pid = int(rest)
            self._started.set()
        elif kind == "error":
            code_text, _, message = rest.partition(" ")
            code = int(code_text) if code_text.isdigit() else None
            self._error = SpawnError(
                self._shell, self._path, code or None, message or "exec failed"
            )
            self._started.set()
        elif kind == "exit":
            self._exit = ShellExit(int(rest))
            self._exit_event.set()
        elif kind == "signal":
            self._exit = ShellExit(None, int(rest))
            self._exit_event.set()
        elif kind in ("killed", "done"):
            self._exit_event.set()

    def wait_started(self) -> SpawnError | None:
        self._started.wait()
        return self._error

    def wait_shell(self) -> ShellExit:
        self._exit_event.wait()
        return self._exit or ShellExit(None)

    def terminate_tree(self) -> None:
        with self._lock:
            if self._killed:
                return
            self._killed = True
        if self._popen.poll() is not None:
            return
        try:
            self._popen.send_signal(signal.SIGTERM)  # shim kills all descendants
        except OSError:
            pass

    def wait_terminated(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        try:
            self._popen.wait(max(deadline - time.monotonic(), 0.01))
            return True
        except subprocess.TimeoutExpired:
            pass
        logger.warning("launch shim %d did not exit; killing its process group", self._popen.pid)
        try:
            os.killpg(self._popen.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            self._popen.wait(1)
        except subprocess.TimeoutExpired:
            return False
        return False

    def close(self) -> None:
        for stream in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._popen.poll() is None:
            self.terminate_tree()
            self.wait_terminated(KILL_GRACE_S)


class PosixBackend:
    def __init__(self) -> None:
        self._live: set[PosixProcess] = set()
        self._lock = threading.Lock()

    def spawn(
        self,
        *,
        shell: str,
        args: list[str] | str,
        cwd: str,
        env: dict[str, str],
        stdin: bool,
    ) -> PosixProcess:
        assert isinstance(args, list)
        path = args[0]
        read_fd, write_fd = os.pipe()
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            shim_source(),
            str(write_fd),
            str(os.getpid()),
            "--",
            *args,
        ]
        try:
            popen = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                pass_fds=(write_fd,),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            os.close(read_fd)
            os.close(write_fd)
            raise spawn_error_from_os(shell, path, exc) from None
        os.close(write_fd)
        process = PosixProcess(popen, read_fd, shell, path)
        error = process.wait_started()
        if error is not None:
            process.close()
            raise error
        with self._lock:
            self._live.add(process)
        return process

    def forget(self, process: PosixProcess) -> None:
        with self._lock:
            self._live.discard(process)

    def shutdown(self) -> None:
        with self._lock:
            live = list(self._live)
        for process in live:
            process.terminate_tree()
        for process in live:
            process.wait_terminated(KILL_GRACE_S)
