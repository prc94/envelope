"""Quiet-process hints require observed CPU, output, and port state."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast

from envelope.execution import quiet
from envelope.execution.runner import ManagedProcess


def _managed(*, bytes_written: int = 0, console: str | None = None) -> ManagedProcess:
    now = time.monotonic()
    process = SimpleNamespace(tree_pids=lambda: frozenset({123}))
    if console is not None:
        process.console_line = lambda: console
    return cast(
        ManagedProcess,
        SimpleNamespace(
            finished=False,
            last_output_at=now - 20,
            cpu_quiet_since=now - 20,
            process=process,
            writer=SimpleNamespace(
                meta=SimpleNamespace(streams={"stdout": SimpleNamespace(bytes=bytes_written)})
            ),
        ),
    )


def test_no_output_yet_requires_verified_idle_and_no_port(monkeypatch) -> None:
    managed = _managed()
    monkeypatch.setattr(quiet, "listening_ports", lambda _pids: frozenset())
    hint = quiet.quiet_hint(managed, 10)
    assert hint is not None and hint.id == "no_output_yet" and "buffer" in hint.text
    managed.cpu_quiet_since = None
    assert quiet.quiet_hint(managed, 10) is None
    managed.cpu_quiet_since = time.monotonic() - 20
    managed.writer.meta.streams["stdout"].bytes = 1
    assert quiet.quiet_hint(managed, 10) is None


def test_quiet_listening_server_gets_no_hint(monkeypatch) -> None:
    monkeypatch.setattr(quiet, "listening_ports", lambda _pids: frozenset({8000}))
    assert quiet.quiet_hint(_managed(), 10) is None
    monkeypatch.setattr(quiet, "listening_ports", lambda _pids: None)
    assert quiet.quiet_hint(_managed(), 10) is None


def test_console_prompt_takes_precedence(monkeypatch) -> None:
    monkeypatch.setattr(quiet, "listening_ports", lambda _pids: frozenset())
    hint = quiet.quiet_hint(_managed(console="Password:"), 10)
    assert hint is not None and hint.id == "console_prompt" and "Password:" in hint.text
    assert quiet.quiet_hint(_managed(console="working"), 10).id == "no_output_yet"  # type: ignore[union-attr]
