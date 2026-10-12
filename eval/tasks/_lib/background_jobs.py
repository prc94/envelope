"""Deterministic long-job fixtures for the background-process task group."""

from __future__ import annotations

import hashlib
from pathlib import Path


def delayed_job(out: str, seed: int, salt: str, seconds: int) -> dict[str, object]:
    token = hashlib.sha256(f"{salt}:{seed}".encode()).hexdigest()[:16]
    answer = f"finished-{token}"
    Path(out, "job.py").write_text(
        "import time\n"
        "print('build started', flush=True)\n"
        f"time.sleep({seconds})\n"
        f"print({answer!r}, flush=True)\n",
        encoding="utf-8",
    )
    return {"expect": answer}
