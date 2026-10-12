"""Best-effort CPU accounting for quiet-process hints."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from envelope.execution.backend import Process


def tree_cpu_seconds(process: Process) -> float | None:
    """Cumulative CPU seconds, or None when ownership/accounting is unknown."""
    if sys.platform == "win32":
        sample = getattr(process, "cpu_seconds", None)
        return sample() if sample is not None else None
    if not sys.platform.startswith("linux"):
        return None
    pids = process.tree_pids()
    if not pids:
        return None
    ticks = 0
    found = False
    for pid in pids:
        try:
            raw = Path(f"/proc/{pid}/stat").read_bytes()
            fields = raw[raw.rfind(b")") + 2 :].split()
            ticks += int(fields[11]) + int(fields[12])
            found = True
        except (OSError, ValueError, IndexError):
            continue
    return ticks / os.sysconf("SC_CLK_TCK") if found else None
