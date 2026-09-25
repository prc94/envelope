"""Run one command to completion: spawn, stream output to the spool, enforce the
deadline, and guarantee the process tree is gone before returning."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from envelope.execution.backend import Backend, Process, ShellExit
from envelope.log import logger
from envelope.output.spool import STDERR, STDOUT, SpoolWriter
from envelope.policy.gateway import Permit
from envelope.policy.model import ExecRequest
from envelope.shells.launch import Launch

KILL_WAIT_S = 5.0
DRAIN_WAIT_S = 2.0
READ_SIZE = 65536


@dataclass
class RunRequest:
    shell_id: str
    launch: Launch
    cwd: str
    env: dict[str, str]
    stdin: str | None
    deadline: float
    """``time.monotonic()`` value at which the command is killed."""
    timeout_s: float


@dataclass
class RunOutcome:
    exit_code: int | None
    signal: int | None
    timed_out: bool
    cancelled: bool
    duration_s: float
    tree_terminated: bool | None
    """None when nothing had to be terminated."""
    ps_errors: int | None = None


def _thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def _write_files(files: dict[Path, bytes]) -> None:
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


async def execute(
    backend: Backend,
    request: RunRequest,
    writer: SpoolWriter,
    *,
    permit: Permit,
    on_spawned: Callable[[Process], None] | None = None,
) -> RunOutcome:
    """Execute ``request``; raises SpawnError, or CancelledError after cleanup.

    ``permit`` proves the request passed the execution gateway (policy decision
    recorded); nothing can be started without one.
    """
    if not isinstance(permit, Permit) or permit.decision.blocks:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise RuntimeError("execution requires a permit from the execution gateway")
    if not isinstance(permit.request, ExecRequest) or permit.request.cwd != request.cwd:
        raise RuntimeError("permit does not match the request being executed")
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    _write_files(request.launch.files)
    process: Process = await asyncio.to_thread(
        backend.spawn,
        shell=request.shell_id,
        args=request.launch.args,
        cwd=request.cwd,
        env=request.env,
        stdin=request.stdin is not None,
    )
    if on_spawned is not None:
        on_spawned(process)

    queue: asyncio.Queue[tuple[int, bytes | None]] = asyncio.Queue()

    def pump(stream_id: int, stream: IO[bytes]) -> None:
        try:
            while True:
                chunk = cast(io.BufferedReader, stream).read1(READ_SIZE)
                if not chunk:
                    break
                loop.call_soon_threadsafe(queue.put_nowait, (stream_id, chunk))
        except (OSError, ValueError):
            pass
        finally:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (stream_id, None))
            except RuntimeError:
                pass

    _thread(lambda: pump(STDOUT, process.stdout), "envelope-stdout")
    _thread(lambda: pump(STDERR, process.stderr), "envelope-stderr")

    if request.stdin is not None and process.stdin is not None:
        data = request.stdin.encode("utf-8")
        stdin = process.stdin

        def feed() -> None:
            try:
                stdin.write(data)
                stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stdin.close()
                except (OSError, ValueError):
                    pass

        _thread(feed, "envelope-stdin")

    shell_exit: asyncio.Future[ShellExit] = loop.create_future()

    def wait_shell() -> None:
        result = process.wait_shell()
        loop.call_soon_threadsafe(lambda: shell_exit.done() or shell_exit.set_result(result))

    _thread(wait_shell, "envelope-wait")

    async def consume() -> None:
        open_streams = 2
        while open_streams:
            stream_id, chunk = await queue.get()
            if chunk is None:
                open_streams -= 1
            else:
                writer.add(stream_id, chunk)

    consumer = asyncio.ensure_future(consume())
    completion = asyncio.ensure_future(asyncio.gather(consumer, shell_exit))
    timed_out = cancelled = False
    tree_terminated: bool | None = None
    try:
        try:
            await asyncio.wait_for(
                asyncio.shield(completion), max(request.deadline - time.monotonic(), 0)
            )
        except TimeoutError:
            timed_out = True
        if timed_out:
            tree_terminated = await _terminate(process)
            await _drain(completion)
        else:
            # Shell exited and all output pipes closed; remove detached leftovers.
            process.terminate_tree()
            await asyncio.to_thread(process.wait_terminated, KILL_WAIT_S)
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(_cancel_cleanup(process, completion))
        raise
    finally:
        if not completion.done():
            completion.cancel()
        process.close()
        if cancelled:
            logger.debug("run cancelled after %.1fs", time.monotonic() - started)

    status = (
        shell_exit.result() if shell_exit.done() and not shell_exit.cancelled() else ShellExit(None)
    )
    return RunOutcome(
        exit_code=None if timed_out else status.exit_code,
        signal=None if timed_out else status.signal,
        timed_out=timed_out,
        cancelled=False,
        duration_s=time.monotonic() - started,
        tree_terminated=tree_terminated,
        ps_errors=_read_ps_errors(request.launch),
    )


async def _terminate(process: Process) -> bool:
    process.terminate_tree()
    return await asyncio.to_thread(process.wait_terminated, KILL_WAIT_S)


async def _drain(completion: asyncio.Future[Any]) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(completion), DRAIN_WAIT_S)
    except TimeoutError:
        logger.warning(
            "output pipes still open %.0fs after termination; giving up on them", DRAIN_WAIT_S
        )
    except asyncio.CancelledError:
        pass


async def _cancel_cleanup(process: Process, completion: asyncio.Future[Any]) -> None:
    await _terminate(process)
    await _drain(completion)


def _read_ps_errors(launch: Launch) -> int | None:
    if launch.status_file is None:
        return None
    try:
        return int(launch.status_file.read_text(encoding="ascii", errors="replace").strip() or "0")
    except (OSError, ValueError):
        return None
