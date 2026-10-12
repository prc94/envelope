"""Assess quiet live processes without guessing when observations are missing."""

from __future__ import annotations

import re
import time

from envelope.execution.ports import listening_ports
from envelope.execution.runner import ManagedProcess
from envelope.render.hints import Hint

_PROMPT = re.compile(r"(?:[:?]|\[[yn]/[yn]\])$|password", re.IGNORECASE)


def quiet_hint(managed: ManagedProcess, after_s: float) -> Hint | None:
    if managed.finished or time.monotonic() - managed.last_output_at < after_s:
        return None
    quiet_since = managed.cpu_quiet_since
    if quiet_since is None or time.monotonic() - quiet_since < after_s:
        return None
    pids = managed.process.tree_pids()
    if pids is None:
        return None
    ports = listening_ports(pids)
    if ports is None or ports:
        return None
    console_line = getattr(managed.process, "console_line", None)
    if console_line is not None:
        line = console_line()
        if line and _PROMPT.search(line):
            return Hint(
                "console_prompt",
                f"note: the hidden console shows {line[-120:]!r}; this process appears to wait "
                "for console input that nobody can type",
            )
    if any(stream.bytes for stream in managed.writer.meta.streams.values()):
        return None
    return Hint(
        "no_output_yet",
        "note: this process has produced no output yet; its output may be held in a buffer "
        "because it is not attached to a terminal (try python -u or stdbuf -oL)",
    )
