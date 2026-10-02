"""Read modes of the ``output`` tool: tail, offset/limit and grep with context."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from envelope.output.cleanup import Entry, clean_line, fold
from envelope.output.render import Budget, Style, cleaned, human_bytes, line_cap, render_entry
from envelope.output.spool import STDERR, STDOUT, SpoolView

StreamFilter = Literal["both", "stdout", "stderr"]
DEFAULT_TAIL = 100
DEFAULT_LIMIT = 200


class ViewError(Exception):
    """Invalid read request (e.g. bad regular expression)."""


@dataclass
class View:
    text: str
    structured: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


def _streams(stream: StreamFilter) -> frozenset[int]:
    return {
        "both": frozenset({STDOUT, STDERR}),
        "stdout": frozenset({STDOUT}),
        "stderr": frozenset({STDERR}),
    }[stream]


def _filtered(
    lines: Iterator[tuple[int, int, str]], streams: frozenset[int]
) -> Iterator[tuple[int, int, str]]:
    for line_no, stream, text in lines:
        if stream in streams:
            yield line_no, stream, text


def _header(reader: SpoolView, what: str, stream: StreamFilter) -> str:
    meta = reader.meta
    parts = [
        reader.handle,
        what,
        f"{reader.total_lines:,} lines total ({human_bytes(meta.total_text_bytes)})",
    ]
    if stream != "both":
        parts.append(f"stream {stream}")
    return " | ".join(parts)


def _binary_note(reader: SpoolView, streams: frozenset[int]) -> list[str]:
    notes: list[str] = []
    for sid, name in ((STDOUT, "stdout"), (STDERR, "stderr")):
        summary = reader.meta.streams[name]
        if sid in streams and summary.binary:
            kind = f" ({summary.binary_type})" if summary.binary_type else ""
            notes.append(
                f"note: {name} is binary output of {summary.bytes:,} bytes{kind}; not shown as text"
            )
    return notes


def _style(reader: SpoolView, stream: StreamFilter, max_chars: int) -> Style:
    both = reader.meta.streams["stdout"].lines > 0 and reader.meta.streams["stderr"].lines > 0
    return Style(mark_stderr=both and stream == "both", numbered=True, max_line=line_cap(max_chars))


def _base(reader: SpoolView, mode: str, stream: StreamFilter) -> dict[str, Any]:
    return {
        "handle": reader.handle,
        "mode": mode,
        "stream": stream,
        "total_lines": reader.total_lines,
        "total_bytes": reader.meta.total_text_bytes,
        "dropped_lines": reader.meta.dropped_lines,
    }


def tail_view(
    reader: SpoolView, n: int, stream: StreamFilter, max_chars: int, max_lines: int
) -> View:
    streams = _streams(stream)
    style = _style(reader, stream, max_chars)
    budget = Budget(max_chars, max_lines)
    taken: list[tuple[Entry, str]] = []
    raw_count = 0
    budget_hit = False
    for entry in fold(cleaned(_filtered(reader.lines_reversed(), streams)), reverse=True):
        if raw_count >= n:
            break
        text = render_entry(entry, style)
        if not budget.fits(text):
            budget_hit = True
            break
        budget.take(text)
        taken.append((entry, text))
        raw_count += entry.count
    taken.reverse()
    return _finish_range(reader, taken, stream, "tail", n_requested=n, budget_hit=budget_hit)


def range_view(
    reader: SpoolView,
    offset: int,
    limit: int,
    stream: StreamFilter,
    max_chars: int,
    max_lines: int,
) -> View:
    streams = _streams(stream)
    style = _style(reader, stream, max_chars)
    budget = Budget(max_chars, max_lines)
    taken: list[tuple[Entry, str]] = []
    end = offset + limit - 1
    budget_hit = False
    for entry in fold(cleaned(_filtered(reader.lines(offset, end), streams))):
        text = render_entry(entry, style)
        if not budget.fits(text):
            budget_hit = True
            break
        budget.take(text)
        taken.append((entry, text))
    return _finish_range(
        reader, taken, stream, "range", n_requested=limit, budget_hit=budget_hit, offset=offset
    )


def _finish_range(
    reader: SpoolView,
    taken: list[tuple[Entry, str]],
    stream: StreamFilter,
    mode: str,
    *,
    n_requested: int,
    budget_hit: bool,
    offset: int | None = None,
) -> View:
    streams = _streams(stream)
    structured = _base(reader, mode, stream)
    lines: list[str] = []
    if taken:
        first, last = taken[0][0].line_no, taken[-1][0].last_line_no
        what = f"lines {first:,}-{last:,}"
        structured.update(first_line=first, last_line=last, shown_entries=len(taken))
    elif offset is not None and offset > reader.total_lines:
        what = f"no lines at offset {offset:,}"
    else:
        what = "no lines"
    lines.append(_header(reader, what, stream))
    dropped = reader.dropped_range()
    if taken:
        previous = taken[0][0].line_no - 1
        for entry, text in taken:
            if dropped and previous < dropped[0] <= entry.line_no - 1:
                lines.append(
                    f"[... lines {dropped[0]:,}-{dropped[1]:,} were dropped (spool cap) ...]"
                )
            lines.append(text)
            previous = entry.last_line_no
    elif offset is None or offset <= reader.total_lines:
        lines.append("(no output)" if reader.total_lines == 0 else "(no lines in this range)")
    lines.extend(_binary_note(reader, streams))
    structured["truncated"] = budget_hit
    if budget_hit and taken:
        last = taken[-1][0].last_line_no
        first = taken[0][0].line_no
        if mode in ("range", "unread"):
            lines.append(
                f'note: output budget reached; continue with output(handle="{reader.handle}", '
                f"offset={last + 1}, limit={DEFAULT_LIMIT})"
            )
        else:
            lines.append(
                f'note: output budget reached; earlier lines via output(handle="{reader.handle}", '
                f"offset={max(first - DEFAULT_LIMIT, 1)}, limit={DEFAULT_LIMIT})"
            )
    return View("\n".join(lines), structured)


def unread_view(
    reader: SpoolView, position: int, stream: StreamFilter, max_chars: int, max_lines: int
) -> View:
    """Lines after ``position`` (the last line previously shown to the model
    in this mode), reporting a dropped range the position falls at or before
    and never pointing a later omission marker into it. The head/tail
    renderer's single-gap assumption does not hold for an arbitrary starting
    position, so this handles the one possible gap itself, up front, before
    the rest of the read is entirely retained and safe to render normally."""
    start = position + 1
    prefix_note: str | None = None
    dropped = reader.dropped_range()
    if dropped and start <= dropped[1]:
        prefix_note = (
            f"lines {dropped[0]:,}-{dropped[1]:,} were dropped (spool cap) and cannot be retrieved"
        )
        start = dropped[1] + 1
    streams = _streams(stream)
    style = _style(reader, stream, max_chars)
    budget = Budget(max_chars, max_lines)
    taken: list[tuple[Entry, str]] = []
    budget_hit = False
    for entry in fold(cleaned(_filtered(reader.lines(start, reader.total_lines), streams))):
        text = render_entry(entry, style)
        if not budget.fits(text):
            budget_hit = True
            break
        budget.take(text)
        taken.append((entry, text))
    view = _finish_range(
        reader, taken, stream, "unread", n_requested=0, budget_hit=budget_hit, offset=start
    )
    if prefix_note:
        lines = view.text.split("\n")
        lines.insert(1, f"[... {prefix_note} ...]")
        view = View("\n".join(lines), view.structured)
    return view


def grep_view(
    reader: SpoolView,
    pattern: str,
    context: int,
    stream: StreamFilter,
    max_chars: int,
    max_lines: int,
) -> View:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ViewError(f"invalid regular expression {pattern!r}: {exc}") from None
    streams = _streams(stream)
    context = max(context, 0)
    matches: list[int] = []
    for line_no, _stream, text in cleaned(_filtered(reader.lines(), streams)):
        if regex.search(text):
            matches.append(line_no)

    style = _style(reader, stream, max_chars)
    style.context_mode = context > 0
    match_set = frozenset(matches)
    style.match_lines = match_set
    windows: list[tuple[int, int]] = []
    for line_no in matches:
        lo, hi = max(line_no - context, 1), min(line_no + context, reader.total_lines)
        if windows and lo <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
        else:
            windows.append((lo, hi))

    budget = Budget(max_chars, max_lines)
    body: list[str] = []
    shown: set[int] = set()
    budget_hit = False
    for lo, hi in windows:
        if body and context > 0:
            if not budget.fits("--"):
                budget_hit = True
                break
            budget.take("--")
            body.append("--")
        for line_no, sid, text in _filtered(reader.lines(lo, hi), streams):
            rendered = render_entry(Entry(line_no, sid, clean_line(text)), style)
            if not budget.fits(rendered):
                budget_hit = True
                break
            budget.take(rendered)
            body.append(rendered)
            if line_no in match_set:
                shown.add(line_no)
        if budget_hit:
            break
    if body and body[-1] == "--":
        body.pop()
    shown_matches = len(shown)
    more = len(matches) - shown_matches
    what = f"grep {pattern!r}: {len(matches):,} matching line{'s' if len(matches) != 1 else ''}"
    lines = [_header(reader, what, stream)]
    lines.extend(body if body else ["(no matches)"])
    if more > 0:
        next_match = next(m for m in matches if m not in shown)
        lines.append(
            f"note: {more:,} more matches beyond the output budget; continue with "
            f'output(handle="{reader.handle}", grep={pattern!r}) after narrowing the pattern, or '
            f'output(handle="{reader.handle}", offset={max(next_match - context, 1)}, '
            f"limit={DEFAULT_LIMIT})"
        )
    lines.extend(_binary_note(reader, streams))
    structured = _base(reader, "grep", stream)
    structured.update(
        pattern=pattern,
        context=context,
        matches=len(matches),
        shown_matches=shown_matches,
        more_matches=more > 0,
        truncated=budget_hit,
    )
    return View("\n".join(lines), structured)
