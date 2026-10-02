"""Unit tests for the `ManagedProcess` split (task 4.1): `start()` hands back
a process already being driven to completion by its own background task, so
a caller's `wait()` is just an observer -- cancelling it never reaches the
process, unlike cancelling the original monolithic `execute()`."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from envelope.audit import AuditLog
from envelope.config.settings import Settings
from envelope.execution.posix import PosixBackend
from envelope.execution.runner import RunRequest, start
from envelope.execution.table import ProcessTable, SlotsRefused
from envelope.output.spool import SpoolManager, SpoolWriter
from envelope.policy.decide import make_decide
from envelope.policy.gateway import Gateway, Permit
from envelope.policy.model import ExecRequest
from envelope.shells.launch import Launch

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX backend")


def _permit(tmp_path: Path, cwd: str) -> Permit:
    gateway = Gateway(Settings(), AuditLog(tmp_path / "audit.jsonl"), make_decide(windows=False))
    request = ExecRequest(shell="sh", shell_path="/bin/sh", command="test", cwd=cwd)
    permit = gateway.authorize(request)
    assert isinstance(permit, Permit)
    return permit


def _spool(tmp_path: Path) -> SpoolWriter:
    mgr = SpoolManager(
        tmp_path / "spool", cap_bytes=1_000_000, total_bytes=10_000_000, retention_s=3600
    )
    return mgr.create({"command": "test"}, fallback_codepage=None)


async def _start_sleep(tmp_path: Path, backend: PosixBackend, script: str, timeout_s: float = 10.0):
    writer = _spool(tmp_path)
    permit = _permit(tmp_path, str(tmp_path))
    deadline = time.monotonic() + timeout_s
    request = RunRequest(
        "sh", Launch(["/bin/sh", "-c", script]), str(tmp_path), {}, None, deadline, timeout_s
    )
    managed = await start(backend, request, writer, permit=permit)
    return managed, writer


def test_cancelling_a_waiter_does_not_stop_the_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = PosixBackend()
        managed, writer = await _start_sleep(tmp_path, backend, "sleep 0.5; echo done")

        waiter = asyncio.ensure_future(managed.wait(time.monotonic() + 5.0))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # cancelling the wait must not have touched the process: it is still
        # running (the 0.5s sleep has not had time to finish yet)
        assert not managed.finished

        finished = await managed.wait(time.monotonic() + 5.0)
        assert finished
        assert managed.outcome is not None
        assert managed.outcome.exit_code == 0
        assert not managed.outcome.timed_out
        writer.finish()

    asyncio.run(scenario())


def test_backends_live_set_shrinks_once_the_process_ends(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = PosixBackend()
        managed, writer = await _start_sleep(tmp_path, backend, "true")
        assert managed.process in backend._live

        finished = await managed.wait(time.monotonic() + 5.0)
        assert finished

        assert managed.process not in backend._live
        writer.finish()

    asyncio.run(scenario())


def test_reserve_refuses_when_the_only_candidate_is_being_waited_on(tmp_path: Path) -> None:
    """design B8: a process with an active waiter is not evictable, so if
    it's the only live process, a new start at the cap is refused rather
    than evicting it out from under whoever is waiting."""

    async def scenario() -> None:
        backend = PosixBackend()
        managed, writer = await _start_sleep(tmp_path, backend, "sleep 2")
        table = ProcessTable(AuditLog(tmp_path / "audit.jsonl"), max_running=1)
        table.register(managed)
        managed.waiters = 1  # simulates its owning call still actively waiting

        with pytest.raises(SlotsRefused):
            await table.reserve()

        managed.waiters = 0
        await managed.terminate()
        await managed.wait(time.monotonic() + 5.0)
        writer.finish()

    asyncio.run(scenario())


def test_reserve_evicts_the_least_recently_used_unwaited_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = PosixBackend()
        old, old_writer = await _start_sleep(tmp_path, backend, "sleep 30")
        old.handed_back = True
        table = ProcessTable(AuditLog(tmp_path / "audit.jsonl"), max_running=1)
        table.register(old)

        victim = await table.reserve()
        assert victim is old
        assert old.evicted is True
        assert old.finished  # reserve() awaits the eviction's tree-exit
        old_writer.finish()

    asyncio.run(scenario())


def test_claim_notice_is_exactly_once_for_concurrent_results(tmp_path: Path) -> None:
    # design B10: two results "completing at once" are really just two
    # synchronous calls in a row on the single-threaded loop -- `claim_notice`
    # must hand the same ended process to only one of them.
    async def scenario() -> None:
        backend = PosixBackend()
        managed, writer = await _start_sleep(tmp_path, backend, "exit 0")
        managed.handed_back = True
        table = ProcessTable(AuditLog(tmp_path / "audit.jsonl"))
        table.register(managed)
        await managed.wait(time.monotonic() + 5.0)

        first = table.claim_notice()
        assert first is managed
        managed.final_seen = True  # what `Services.claim_notice_text()` does next
        second = table.claim_notice()
        assert second is None
        writer.finish()

    asyncio.run(scenario())


def test_leftover_count_suppressed_while_stopping(tmp_path: Path) -> None:
    # design B11/task 7.3: a stop()-induced finish must not report leftovers
    # as an unexpected "background job killed" -- the hint is suppressed
    # entirely by never setting `leftover_count` for a stopping process.
    async def scenario() -> None:
        backend = PosixBackend()
        managed, writer = await _start_sleep(
            tmp_path, backend, "sleep 30 & sleep 30; wait", timeout_s=30
        )
        graceful = await managed.stop(1.0)
        assert graceful is True  # plain `sleep` dies from SIGTERM by default
        assert managed.outcome is not None
        assert managed.outcome.leftover_count is None
        writer.finish()

    asyncio.run(scenario())
