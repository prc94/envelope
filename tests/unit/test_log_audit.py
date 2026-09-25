import json
import sys
from pathlib import Path

import pytest

from envelope.audit import AuditLog, call_record
from envelope.log import logger, set_level


def test_logger_never_writes_stdout(capfd: pytest.CaptureFixture[str]) -> None:
    set_level("debug")
    try:
        logger.debug("debug message")
        logger.info("info message")
        logger.warning("warn message")
        logger.error("error message")
        sys.stdout.flush()
    finally:
        set_level("warn")
    out, err = capfd.readouterr()
    assert out == ""
    for text in ("debug message", "info message", "warn message", "error message"):
        assert text in err


def test_log_level_filters(capfd: pytest.CaptureFixture[str]) -> None:
    set_level("error")
    try:
        logger.warning("hidden")
        logger.error("shown")
    finally:
        set_level("warn")
    _, err = capfd.readouterr()
    assert "hidden" not in err and "shown" in err


def test_audit_record(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    audit = AuditLog(path)
    audit.write(
        call_record(
            client={"name": "test"},
            tool="run",
            arguments={"command": "echo hi"},
            coercions=[],
            request={"kind": "exec", "command": "echo hi"},
            decision={"verdict": "allow", "rule": "default"},
            outcome={"exit_code": 0, "duration_ms": 12},
        )
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "run"
    assert record["decision"]["verdict"] == "allow"
    assert record["outcome"]["exit_code"] == 0
    for key in ("ts", "client", "arguments", "coercions", "request", "outcome"):
        assert key in record


def test_unwritable_audit_log_reports_once(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    audit = AuditLog(blocker / "audit.jsonl")
    audit.write({"event": "a"})
    audit.write({"event": "b"})
    out, err = capfd.readouterr()
    assert out == ""
    assert err.count("not writable") == 1


def test_disabled_audit_log_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path, enabled=False).write({"event": "x"})
    assert not path.exists()
