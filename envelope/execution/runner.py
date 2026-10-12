"""Start a command and let a ``ManagedProcess`` own its lifecycle -- spawn,
pumps, consumer, shell-wait, deadline and cleanup -- independently of
whichever call is currently waiting on it (design B3).

``start()`` does the synchronous setup (permit check, spawn, pump/consumer/
shell-wait threads and tasks) and returns a ``ManagedProcess`` immediately;
a background task then owns the process from there until it finishes,
computing its ``RunOutcome``. ``ManagedProcess.wait()`` only observes that
task's completion -- cancelling a wait ends only the wait, never the
process, because the background task is not part of the awaiting call's
task and so is unaffected by its cancellation.
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, cast

from envelope.execution.backend import Backend, Process, ShellExit
from envelope.execution.ports import listening_ports, port_reachable
from envelope.execution.wait import WaitCondition
from envelope.log import logger
from envelope.output.cleanup import clean_line
from envelope.output.render import cleaned
from envelope.output.spool import STDERR, STDOUT, LiveSpoolReader, SpoolWriter
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
    """``time.monotonic()`` value at which the command is killed by
    ``timeout_s``; ``float('inf')`` when no ``timeout_s`` applies (design B9:
    ``default_timeout_s`` is unset by default)."""
    timeout_s: float | None
    command: str = ""
    """Raw command text, for the same-command duplicate-start note (design B6)."""
    lifetime_deadline: float = float("inf")
    """A second, independent kill deadline from ``max_lifetime_s`` (design
    B9), regardless of ``timeout_s``. ``float('inf')`` when unset."""


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
    lifetime_exceeded: bool = False
    """``max_lifetime_s`` fired rather than ``timeout_s`` (design B9)."""
    leftover_count: int | None = None
    """Descendants still running when the shell exited on its own (not a
    timeout/stop/eviction), terminated as leftovers (design B11); POSIX/Linux
    only, and only for a plain finish -- ``None`` otherwise."""


def _thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def _write_files(files: dict[Path, bytes]) -> None:
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class ManagedProcess:
    """Owns a started command from ``start()`` until it finishes.

    ``outcome`` is ``None`` until the background finishing task completes;
    ``wait()`` is the only thing a tool call awaits, and cancelling it never
    reaches the process -- the finishing task is a separate, independently
    scheduled task that a wait's own cancellation does not touch.

    All mutation (``name``, ``waiters``, ``stopping``, ``final_seen``,
    ``last_use``) happens on the event loop, per design B3; there is no lock
    because nothing here ever awaits between a check and an update of them.
    """

    def __init__(
        self,
        backend: Backend,
        process: Process,
        writer: SpoolWriter,
        request: RunRequest,
    ) -> None:
        self.backend = backend
        self.process = process
        self.writer = writer
        self.request = request
        self.handle = writer.meta.handle
        self.name: str | None = None
        self.started = time.monotonic()
        self.last_use = self.started
        self.waiters = 0
        self.stopping = False
        self.final_seen = False
        self.evicted = False
        """Set by the process table just before it stops this process to
        free a slot for a new one (design B8)."""
        self.handed_back = False
        """Set once a `run` call has yielded this process with a handle
        (task 4.3); gates the proactive end-of-process audit (task 4.4) --
        a process that finishes inside its own original call is reported by
        that call, not again here."""
        self.on_finish: Callable[[ManagedProcess], None] | None = None
        """Set by the process table at registration; called once, after
        ``outcome`` is set, if this process was ever handed back."""
        self.outcome: RunOutcome | None = None
        self.last_output_at = self.started
        """Updated whenever a chunk of output arrives (design B5: `idle:`
        measures from the later of a wait's start and this)."""
        self.port_verified: bool | None = None
        """Set by the most recent `port:` wait that matched: ``True`` when
        ownership was attributed to this tree, ``False`` when only the
        unverified `connect` fallback succeeded (design B5)."""
        self.cpu_quiet_since: float | None = None
        """Set by the process table's periodic CPU sampler; None means unknown."""
        self.cpu_last_seconds: float | None = None
        self._done = asyncio.Event()
        self._finisher: asyncio.Task[None] | None = None

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    async def wait(self, deadline: float, *, condition: str = "exit") -> bool:
        """Wait for ``condition`` (only ``"exit"`` is implemented so far;
        the rest arrive with the wait-condition tasks) until ``deadline``.
        Returns whether it was met. Cancelling this call ends only the
        wait -- the process keeps running under the finishing task."""
        if condition != "exit":
            raise NotImplementedError(f"wait condition {condition!r} is not implemented yet")
        self.last_use = time.monotonic()
        if self._done.is_set():
            return True
        self.waiters += 1
        try:
            remaining = max(deadline - time.monotonic(), 0)
            if remaining == float("inf"):
                await self._done.wait()
            else:
                try:
                    await asyncio.wait_for(self._done.wait(), remaining)
                except TimeoutError:
                    return False
            return True
        finally:
            self.waiters -= 1
            self.last_use = time.monotonic()

    async def wait_condition(
        self, condition: WaitCondition, deadline: float, *, from_line: int = 0
    ) -> Literal["condition_met", "finished", "wait_expired"]:
        """Wait for ``condition`` until ``deadline`` (design B5): ``exit``
        delegates to ``wait()``; ``regex:``/``idle:``/``port:`` poll (0.1s,
        0.1s and 0.5s respectively) since they have no single future to await.
        Returns which of the three outcomes ended the wait. ``from_line`` is
        the line count the caller has already considered (0 for `run`, which
        considers all output; `output`'s unread-backlog position otherwise).
        Cancelling this call ends only the wait -- the process keeps running
        under the finishing task."""
        if condition.kind == "exit":
            met = await self.wait(deadline)
            return "condition_met" if met else "wait_expired"
        self.last_use = time.monotonic()
        wait_started = time.monotonic()
        self.waiters += 1
        try:
            poll_s = 0.5 if condition.kind == "port" else 0.1
            while True:
                if self._condition_met(condition, from_line, wait_started):
                    return "condition_met"
                if self.finished:
                    return "finished"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "wait_expired"
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._done.wait()), min(poll_s, remaining)
                    )
                except TimeoutError:
                    pass
        finally:
            self.waiters -= 1
            self.last_use = time.monotonic()

    def _condition_met(self, condition: WaitCondition, from_line: int, wait_started: float) -> bool:
        if condition.kind == "regex":
            assert condition.pattern is not None
            return self._regex_matches(condition.pattern, from_line)
        if condition.kind == "idle":
            assert condition.idle_s is not None
            quiet_since = max(wait_started, self.last_output_at)
            return time.monotonic() - quiet_since >= condition.idle_s
        if condition.kind == "port":
            assert condition.port is not None
            pids = self.process.tree_pids()
            if pids is not None:
                ports = listening_ports(pids)
                if ports is not None:
                    met = condition.port in ports
                    if met:
                        self.port_verified = True
                    return met
            # Ownership cannot be verified on this platform/process (design
            # B5: macOS, a Linux `hidepid` mount, or a lookup failure) --
            # fall back to an unverified local connection.
            met = port_reachable(condition.port)
            if met:
                self.port_verified = False
            return met
        return False

    def _regex_matches(self, pattern: re.Pattern[str], from_line: int) -> bool:
        reader = LiveSpoolReader(self.writer.dir, self.writer)
        for _, _, text in cleaned(reader.lines(start=from_line + 1)):
            if pattern.search(text):
                return True
        for text in reader.unterminated_lines().values():
            if pattern.search(clean_line(text)):
                return True
        return False

    async def terminate(self) -> bool:
        """Force the whole tree to die now (idempotent). Used when a
        deadline passes; the finishing task's own cleanup still runs
        afterward and sets ``outcome``/``wait()``'s completion."""
        return await _terminate(self.process)

    async def stop(self, grace_s: float) -> bool:
        """Request termination of the whole tree, waiting up to ``grace_s``
        for a graceful exit before forcing it (design B7). Returns whether
        the stop was graceful. Sets ``stopping`` immediately, before
        awaiting anything, so a concurrent eviction/completion-report never
        treats this process as a surprise background-job leftover. A no-op,
        returning ``True``, once the process has already finished."""
        self.stopping = True
        if self.finished:
            return True
        requested = self.process.request_graceful_stop()
        if requested and await self.wait(time.monotonic() + grace_s):
            return True
        await self.terminate()
        await self.wait(float("inf"))
        return False

    def begin(self, completion: asyncio.Future[Any], shell_exit: asyncio.Future[ShellExit]) -> None:
        self._finisher = asyncio.ensure_future(self._run(completion, shell_exit))

    async def _run(
        self, completion: asyncio.Future[Any], shell_exit: asyncio.Future[ShellExit]
    ) -> None:
        """Drive this process to its natural end or its deadline, whichever
        comes first, then clean up and record its outcome. Runs as its own
        task, so nothing that merely awaits ``wait()`` can cancel it; only
        envelope's own shutdown path (task 4.2) cancels this task directly."""
        process = self.process
        request = self.request
        timed_out = lifetime_exceeded = cancelled = False
        tree_terminated: bool | None = None
        leftover_count: int | None = None
        effective_deadline = min(request.deadline, request.lifetime_deadline)
        try:
            try:
                if effective_deadline == float("inf"):
                    await asyncio.shield(completion)
                else:
                    await asyncio.wait_for(
                        asyncio.shield(completion), max(effective_deadline - time.monotonic(), 0)
                    )
            except TimeoutError:
                if request.lifetime_deadline < request.deadline:
                    lifetime_exceeded = True
                else:
                    timed_out = True
            if timed_out or lifetime_exceeded:
                tree_terminated = await _terminate(process)
                await _drain(completion)
            else:
                # Shell exited and all output pipes closed; remove detached leftovers.
                process.terminate_tree()
                await asyncio.to_thread(process.wait_terminated, KILL_WAIT_S)
                if not self.stopping:
                    # While a `stop`/eviction is in progress this same cleanup
                    # runs too, but it is not an unexpected background job
                    # (design: "While a stop is in progress, envelope SHALL
                    # NOT report the tree's remaining processes as background
                    # processes killed at completion").
                    leftover_count = getattr(process, "leftover_count", None)
        except asyncio.CancelledError:
            cancelled = True
            await asyncio.shield(_cancel_cleanup(process, completion))
            raise
        finally:
            if not completion.done():
                completion.cancel()
            process.close()
            forget = getattr(self.backend, "forget", None)
            if forget is not None:
                forget(process)
            if cancelled:
                logger.debug("run cancelled after %.1fs", time.monotonic() - self.started)

        status = (
            shell_exit.result()
            if shell_exit.done() and not shell_exit.cancelled()
            else ShellExit(None)
        )
        ended = timed_out or lifetime_exceeded
        self.outcome = RunOutcome(
            exit_code=None if ended else status.exit_code,
            signal=None if ended else status.signal,
            timed_out=timed_out,
            cancelled=False,
            duration_s=time.monotonic() - self.started,
            tree_terminated=tree_terminated,
            ps_errors=_read_ps_errors(request.launch),
            lifetime_exceeded=lifetime_exceeded,
            leftover_count=leftover_count,
        )
        self._done.set()
        if self.handed_back and self.on_finish is not None:
            self.on_finish(self)


async def start(
    backend: Backend,
    request: RunRequest,
    writer: SpoolWriter,
    *,
    permit: Permit,
    on_spawned: Callable[[Process], None] | None = None,
) -> ManagedProcess:
    """Spawn ``request`` and start its pumps, consumer and shell-wait; the
    returned ``ManagedProcess`` is already being driven to completion by a
    background task by the time this returns. Raises ``SpawnError``.

    ``permit`` proves the request passed the execution gateway (policy
    decision recorded); nothing can be started without one.
    """
    if not isinstance(permit, Permit) or permit.decision.blocks:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise RuntimeError("execution requires a permit from the execution gateway")
    if not isinstance(permit.request, ExecRequest) or permit.request.cwd != request.cwd:
        raise RuntimeError("permit does not match the request being executed")
    loop = asyncio.get_running_loop()
    _write_files(request.launch.files)
    # POSIX: the shim arms PDEATHSIG against *this* thread's death (spike S3),
    # so spawning must stay on asyncio's default executor's persistent worker
    # threads -- never a throwaway thread that exits right after this call --
    # or a crash of envelope could leave the child without its watchdog.
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

    managed = ManagedProcess(backend, process, writer, request)

    async def consume() -> None:
        open_streams = 2
        while open_streams:
            stream_id, chunk = await queue.get()
            if chunk is None:
                open_streams -= 1
            else:
                writer.add(stream_id, chunk)
                managed.last_output_at = time.monotonic()

    consumer = asyncio.ensure_future(consume())
    completion = asyncio.ensure_future(asyncio.gather(consumer, shell_exit))

    managed.begin(completion, shell_exit)
    return managed


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
