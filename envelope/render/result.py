"""Model-facing result layout: header, truncation line, body, notes; plus the
metadata-only ``structuredContent``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from envelope.output.render import Rendered, human_bytes
from envelope.output.spool import SpoolMeta
from envelope.render.hints import SIGNAL_NAMES, Hint


@dataclass
class RunReport:
    shell: str
    cwd: str
    duration_s: float
    exit_code: int | None
    signal: int | None = None
    timed_out: bool = False
    timeout_s: float = 0.0
    cancelled: bool = False
    running: bool = False
    """Handed back while still running (design B1); ``exit_code``/``signal``
    stay ``None`` and ``meta``/``rendered`` reflect the spool so far."""
    unterminated_lines: Sequence[str] = ()
    """Each stream's current no-newline-yet line, pre-formatted; only set
    when ``running`` (design B4)."""
    meta: SpoolMeta | None = None
    rendered: Rendered | None = None
    hints: Sequence[Hint] = ()
    notes: Sequence[str] = ()
    decision: dict[str, Any] | None = None
    end_reason: str | None = None
    """``exit``, ``timeout``, ``lifetime``, ``stop``, ``eviction``, ``cancel``
    or ``shutdown`` (design B15); unset for a running result."""
    name: str | None = None
    graceful: bool | None = None
    """Whether a `stop` was graceful or forced; unset outside `stop`."""
    extra: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


def format_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.2f}s"
    if seconds < 600:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def status_text(report: RunReport) -> str:
    if report.running:
        return "running"
    if report.cancelled:
        return "cancelled"
    if report.timed_out:
        return f"timed out after {report.timeout_s:g}s"
    if report.signal is not None:
        return f"signal {report.signal} ({SIGNAL_NAMES.get(report.signal, 'unknown')})"
    if report.exit_code is None:
        return "exit ?"
    code = report.exit_code
    if code > 0xFFFF:
        return f"exit 0x{code & 0xFFFFFFFF:08X}"
    return f"exit {code}"


def header(report: RunReport) -> str:
    return f"{status_text(report)} | {format_duration(report.duration_s)} | {report.shell} | cwd {report.cwd}"


def _ranges(shown: Sequence[tuple[int, int]]) -> str:
    return " and ".join(f"{a:,}-{b:,}" if a != b else f"{a:,}" for a, b in shown)


def truncation_line(meta: SpoolMeta, rendered: Rendered) -> str | None:
    if not rendered.truncated and not meta.dropped_raw_bytes:
        return None
    text = (
        f"output: {meta.total_lines:,} lines, {human_bytes(meta.total_text_bytes)}; "
        f"showing lines {_ranges(rendered.shown) or 'none'}; handle {meta.handle}"
    )
    if rendered.long_lines_cut and rendered.omitted_lines == 0:
        text += " (long lines shortened)"
    if meta.dropped_raw_bytes:
        text += (
            f"; {human_bytes(meta.dropped_raw_bytes)} dropped from the middle of the spool (cap)"
        )
    return text


def run_text(report: RunReport) -> str:
    parts = [header(report)]
    meta, rendered = report.meta, report.rendered
    if meta is not None and rendered is not None:
        if (line := truncation_line(meta, rendered)) is not None:
            parts.append(line)
        binary = [
            (name, s.bytes, s.binary_type)
            for name, s in meta.streams.items()
            if s.binary and s.bytes
        ]
        if rendered.body:
            parts.append(rendered.body)
        for name, count, kind in binary:
            parts.append(
                f"[binary output on {name}: {count:,} bytes{f' ({kind})' if kind else ''}]"
            )
        if not rendered.body and not binary and not (report.running and report.unterminated_lines):
            parts.append("(no output)")
    else:
        parts.append("(no output)")
    parts.extend(report.unterminated_lines)
    parts.extend(report.notes)
    parts.extend(hint.text for hint in report.hints)
    return "\n".join(parts)


def run_structured(report: RunReport) -> dict[str, Any]:
    data: dict[str, Any] = {
        "state": "running" if report.running else "finished",
        "exit_code": report.exit_code,
        "signal": report.signal,
        "timed_out": report.timed_out,
        "cancelled": report.cancelled,
        "duration_ms": round(report.duration_s * 1000),
        "shell": report.shell,
        "cwd": report.cwd,
    }
    meta, rendered = report.meta, report.rendered
    if meta is not None:
        data["handle"] = meta.handle
        data["streams"] = {
            name: {
                "lines": s.lines,
                "bytes": s.bytes,
                **({"binary": s.binary_type or True} if s.binary else {}),
            }
            for name, s in meta.streams.items()
        }
        data["total_lines"] = meta.total_lines
        if meta.dropped_raw_bytes:
            data["dropped_bytes"] = meta.dropped_raw_bytes
        decoding = {
            name: s.encoding
            for name, s in meta.streams.items()
            if s.encoding not in (None, "utf-8")
        }
        replaced = sum(s.replaced for s in meta.streams.values())
        if decoding or replaced:
            data["decoding"] = {**decoding, **({"replaced": replaced} if replaced else {})}
    if rendered is not None:
        data["shown_lines"] = rendered.shown_lines
        data["truncated"] = rendered.truncated
    if report.hints:
        data["hints"] = [hint.id for hint in report.hints]
    if report.decision is not None:
        data["decision"] = report.decision
    if report.end_reason is not None:
        data["end_reason"] = report.end_reason
    if report.name is not None:
        data["name"] = report.name
    if report.graceful is not None:
        data["graceful"] = report.graceful
    data.update(report.extra)
    return data
