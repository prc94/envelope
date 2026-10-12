"""The process table: every live or recently-ended process, keyed by handle
(design B3). Mutated only on the event loop. ``start()`` registers a process;
the tool call that started it deregisters it once it has reported the
outcome normally. Once a process has been handed back (task 4.3), its own
natural end (exit, `timeout_s`, `max_lifetime_s`) is no longer inside any
tool call, so the table audits it proactively via `ManagedProcess.on_finish`
instead of waiting for `shutdown()`. ``shutdown()`` sweeps whatever is left
at exit -- a process whose owning call was cancelled before it could
report, or one nobody is currently watching -- terminating it if still
alive and writing the ``process_end`` record nothing else got to write.
"""

from __future__ import annotations

import asyncio
import re
import time

from envelope.audit import AuditLog
from envelope.execution.cpu import tree_cpu_seconds
from envelope.execution.runner import ManagedProcess

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
HANDLE_SHAPE_RE = re.compile(r"^out_[0-9a-f]{8}$")


def validate_name(name: str) -> str | None:
    """Design B6: 1-40 chars of letters/digits/`.`/`_`/`-`, not shaped like a
    handle. Returns an error message, or ``None`` if ``name`` is valid."""
    if not NAME_RE.match(name):
        return (
            f"`name` {name!r} is invalid: use 1-40 characters of letters, digits, '.', '_' or '-'"
        )
    if HANDLE_SHAPE_RE.match(name):
        return f"`name` {name!r} looks like an output handle (out_ + 8 hex digits); choose another"
    return None


def end_reason(managed: ManagedProcess) -> str:
    outcome = managed.outcome
    if managed.evicted:
        return "eviction"
    if outcome is None:
        return "shutdown"
    if outcome.lifetime_exceeded:
        return "lifetime"
    if outcome.timed_out:
        return "timeout"
    return "exit"


class SlotsRefused(Exception):
    """The live-process cap is full and nothing could be evicted to make
    room (design B8: every process is named/waited-on, or ``evict_policy``
    is ``"refuse"``)."""

    def __init__(self, max_running: int) -> None:
        self.max_running = max_running
        super().__init__(f"the live-process cap ({max_running}) is full")


class ProcessTable:
    def __init__(
        self, audit: AuditLog, *, max_running: int = 16, evict_policy: str = "lru-unnamed"
    ) -> None:
        self._audit = audit
        self.max_running = max_running
        self.evict_policy = evict_policy
        self._by_handle: dict[str, ManagedProcess] = {}
        self._reserved = 0
        self._by_name: dict[str, ManagedProcess] = {}
        """The currently-live process for a name, if any (design B6)."""
        self._name_handle: dict[str, str] = {}
        """The most recent handle started under a name, kept after the
        process ends so `output`/`stop` can still resolve it by name."""
        self._starting: dict[str, asyncio.Future[ManagedProcess | None]] = {}
        """Set synchronously by `begin_start()` while the first `run(name=…)`
        call for a not-yet-running name is spawning; a concurrent call for
        the same name awaits this instead of starting a second process."""
        self._name_locks: dict[str, asyncio.Lock] = {}
        """Serializes `restart: true` per name (design B6): held across the
        stop of the old process and the start of the new one."""
        self._samplers: set[asyncio.Task[None]] = set()

    def register(self, managed: ManagedProcess) -> None:
        managed.on_finish = self._on_process_finished
        self._by_handle[managed.handle] = managed
        sampler = asyncio.create_task(self._sample_cpu(managed))
        self._samplers.add(sampler)
        sampler.add_done_callback(self._samplers.discard)

    async def _sample_cpu(self, managed: ManagedProcess) -> None:
        """Keep a tri-state quiet interval for live processes (design B12)."""
        while not managed.finished:
            sample = tree_cpu_seconds(managed.process)
            now = time.monotonic()
            previous = managed.cpu_last_seconds
            if sample is None:
                managed.cpu_quiet_since = None
                managed.cpu_last_seconds = None
            elif previous is None or sample > previous:
                managed.cpu_last_seconds = sample
                managed.cpu_quiet_since = now
            await asyncio.sleep(2)

    def forget(self, handle: str) -> None:
        managed = self._by_handle.pop(handle, None)
        if managed is None:
            return
        # Only clear the live pointer if it's still this process: a restart
        # may already have replaced it under the same name.
        if managed.name is not None and self._by_name.get(managed.name) is managed:
            self._by_name.pop(managed.name, None)

    # --- names, joins and restart (design B6) -------------------------------------

    def find_live(self, name: str) -> ManagedProcess | None:
        return self._by_name.get(name)

    def resolve_handle(self, handle_or_name: str) -> str | None:
        """Translate a name to its most recently started handle; a handle
        passes through unchanged. ``None`` if a name is unknown."""
        if HANDLE_SHAPE_RE.match(handle_or_name):
            return handle_or_name
        return self._name_handle.get(handle_or_name)

    def name_lock(self, name: str) -> asyncio.Lock:
        lock = self._name_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._name_locks[name] = lock
        return lock

    def begin_start(self, name: str) -> asyncio.Future[ManagedProcess | None] | None:
        """Synchronous reservation (no ``await`` before or after, on the same
        tick, relative to ``find_live()``): if nobody else is starting
        ``name`` right now, reserve it and return ``None`` (the caller must
        start it and call ``finish_start()``); otherwise return the future to
        await, then join its result if not ``None``."""
        existing = self._starting.get(name)
        if existing is not None:
            return existing
        self._starting[name] = asyncio.get_running_loop().create_future()
        return None

    def finish_start(self, name: str, managed: ManagedProcess | None) -> None:
        """Resolve a pending ``begin_start()`` reservation. ``managed`` is
        ``None`` when the start failed -- concurrent joiners then see ``None``
        and fall back to starting themselves."""
        future = self._starting.pop(name, None)
        if managed is not None:
            self.assign_name(name, managed)
        if future is not None and not future.done():
            future.set_result(managed)

    def assign_name(self, name: str, managed: ManagedProcess) -> None:
        managed.name = name
        self._by_name[name] = managed
        self._name_handle[name] = managed.handle

    # --- live-process cap (design B8) ---------------------------------------------

    def _live_count(self) -> int:
        return sum(1 for m in self._by_handle.values() if not m.finished) + self._reserved

    def _victim(self) -> ManagedProcess | None:
        candidates = [
            m
            for m in self._by_handle.values()
            if m.name is None and m.waiters == 0 and not m.stopping and not m.finished
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.last_use)

    async def reserve(self) -> ManagedProcess | None:
        """Reserve a live-process slot for a new start, synchronously if one
        is already free. Otherwise evicts the least-recently-used unnamed,
        unwaited process and awaits its tree's exit before returning.
        Raises ``SlotsRefused`` if the cap is full and nothing is evictable,
        or ``evict_policy`` is ``"refuse"``. Call ``release()`` if the start
        then fails before ``register()``, or ``register()`` itself once it
        succeeds -- both consume the reservation."""
        if self._live_count() < self.max_running:
            self._reserved += 1
            return None
        victim = self._victim()
        if victim is None or self.evict_policy == "refuse":
            raise SlotsRefused(self.max_running)
        victim.evicted = True
        victim.stopping = True
        await victim.terminate()
        await victim.wait(float("inf"))
        self._reserved += 1
        return victim

    def release(self) -> None:
        """Release a reservation that was never turned into a registered
        process (the spawn itself failed)."""
        self._reserved = max(self._reserved - 1, 0)

    def commit(self, managed: ManagedProcess) -> None:
        """Turn a successful ``reserve()`` into a registered process."""
        self._reserved = max(self._reserved - 1, 0)
        self.register(managed)

    def _on_process_finished(self, managed: ManagedProcess) -> None:
        """Called by ``ManagedProcess._run()`` itself when a handed-back
        process ends on its own -- nobody's tool call is going to report it,
        so this is the only place its end gets audited. It stays in the
        table (no longer counted as live, `_live_count` above) so `processes`
        can still list it as finished-and-unseen; whichever call later
        observes it (`run` join, `output`, `stop` or `processes`) forgets it
        via `Services.finalize_process`."""
        if managed.handle not in self._by_handle:
            return  # already forgotten (e.g. a concurrent shutdown swept it)
        self._write_process_end(managed, end_reason(managed))

    def get(self, handle: str) -> ManagedProcess | None:
        return self._by_handle.get(handle)

    def all(self) -> list[ManagedProcess]:
        return list(self._by_handle.values())

    def claim_notice(self) -> ManagedProcess | None:
        """Design B10: one ended process the model has not seen the final
        status of, if any. Synchronous (no `await` before the caller marks it
        `final_seen`), so two results returning at once cannot both claim the
        same one."""
        for managed in self._by_handle.values():
            if managed.finished and not managed.final_seen:
                return managed
        return None

    async def shutdown(self, reason: str = "shutdown") -> None:
        """Terminate every still-live process and audit its end; called once,
        after every in-flight tool call has already been cancelled and
        awaited, so anything still here was not reported by its own call."""
        for managed in list(self._by_handle.values()):
            if not managed.finished:
                await managed.terminate()
                await managed.wait(float("inf"))
            self._write_process_end(managed, reason)
            self.forget(managed.handle)

    def _write_process_end(self, managed: ManagedProcess, reason: str) -> None:
        outcome = managed.outcome
        duration_s = outcome.duration_s if outcome else time.monotonic() - managed.started
        self._audit.write(
            {
                "event": "process_end",
                "handle": managed.handle,
                "name": managed.name,
                "end_reason": reason,
                "exit_code": outcome.exit_code if outcome else None,
                "signal": outcome.signal if outcome else None,
                "duration_ms": round(duration_s * 1000),
            }
        )
