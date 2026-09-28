"""Secret-safe credential scanning shared by the runner and gateway."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def scan_credentials(root: str | Path, credential: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not credential:
        return findings
    needle = credential.encode()
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        start = 0
        while (offset := data.find(needle, start)) >= 0:
            findings.append({"file": str(path), "offset": offset})
            start = offset + 1
    return findings
