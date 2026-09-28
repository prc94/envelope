"""Durable JSONL trial results."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class ResultWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        value = {"schema_version": SCHEMA_VERSION, **record}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def read_results(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records, malformed = [], []
    if not Path(path).exists():
        return records, malformed  # a job that stopped before its first trial
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            malformed.append({"line": number, "error": str(exc)})
    return records, malformed
