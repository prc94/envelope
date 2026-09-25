"""Append-only JSONL audit log of every tool call and policy decision."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from envelope.log import logger


class AuditLog:
    """Writes one JSON object per line. Failures never propagate to callers."""

    def __init__(self, path: Path | None, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled and path is not None
        self._lock = threading.Lock()
        self._failed = False

    def write(self, record: dict[str, Any]) -> None:
        if not self.enabled or self.path is None:
            return
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **record}
        line = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                if not self._failed:
                    self._failed = True
                    logger.error(
                        "audit log %s is not writable (%s); continuing without audit records",
                        self.path,
                        exc.strerror or exc,
                    )


def call_record(
    *,
    client: dict[str, Any] | None,
    tool: str,
    arguments: dict[str, Any],
    coercions: list[dict[str, Any]],
    request: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    outcome: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    """Build a record following the configuration spec's audit schema."""
    return {
        **extra,
        "event": "tool_call",
        "client": client,
        "tool": tool,
        "arguments": arguments,
        "coercions": coercions,
        "request": request,
        "decision": decision,
        "outcome": outcome,
    }
