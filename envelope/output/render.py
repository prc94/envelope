"""Budgeted rendering of spooled output (head + tail with an omission marker)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from envelope.output.cleanup import Entry, clean_line, fold
from envelope.output.spool import STDERR, SpoolView

STDERR_MARK = "[stderr] "
HEAD_SHARE = 1 / 3
MARKER_RESERVE = 300


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{n} B"


def line_cap(max_chars: int) -> int:
    return min(2000, max(200, max_chars // 4))


@dataclass
class Budget:
    max_chars: int
    max_lines: int
    chars: int = 0
    lines: int = 0

    def fits(self, text: str) -> bool:
        return self.lines + 1 <= self.max_lines and self.chars + len(text) + 1 <= self.max_chars

    def take(self, text: str) -> None:
        self.lines += 1
        self.chars += len(text) + 1


@dataclass
class Style:
    mark_stderr: bool = False
    numbered: bool = False
    max_line: int = 2000
    match_lines: frozenset[int] = frozenset()
    context_mode: bool = False


def render_entry(entry: Entry, style: Style) -> str:
    text = entry.text
    if len(text) > style.max_line:
        extra = len(text) - style.max_line
        text = f"{text[: style.max_line]} [... {extra:,} more chars in line {entry.line_no}]"
    if style.mark_stderr and entry.stream == STDERR:
        text = STDERR_MARK + text
    if style.numbered:
        label = f"{entry.line_no}-{entry.last_line_no}" if entry.count > 1 else str(entry.line_no)
        sep = "- " if style.context_mode and entry.line_no not in style.match_lines else ": "
        text = f"{label}{sep}{text}"
    if entry.count > 1:
        text += f"  [line repeated {entry.count} times]"
    return text


def cleaned(lines: Iterable[tuple[int, int, str]]) -> Iterator[tuple[int, int, str]]:
    for line_no, stream, text in lines:
        yield line_no, stream, clean_line(text)


@dataclass
class Rendered:
    body: str
    truncated: bool = False
    shown: list[tuple[int, int]] = field(default_factory=lambda: list[tuple[int, int]]())
    shown_lines: int = 0
    omitted_lines: int = 0
    omitted_bytes: int = 0
    long_lines_cut: bool = False


def _take(
    entries: Iterable[Entry], budget: Budget, style: Style
) -> tuple[list[tuple[Entry, str]], bool]:
    taken: list[tuple[Entry, str]] = []
    for entry in entries:
        text = render_entry(entry, style)
        if not budget.fits(text):
            return taken, False
        budget.take(text)
        taken.append((entry, text))
    return taken, True


def _span(entries: list[tuple[Entry, str]]) -> tuple[int, int] | None:
    if not entries:
        return None
    return entries[0][0].line_no, entries[-1][0].last_line_no


def render_run_output(reader: SpoolView, max_chars: int, max_lines: int) -> Rendered:
    """Render cleaned, folded output within the budget, favoring the tail."""
    meta = reader.meta
    style = Style(
        mark_stderr=meta.streams["stdout"].lines > 0 and meta.streams["stderr"].lines > 0,
        max_line=line_cap(max_chars),
    )
    total = reader.total_lines
    if total == 0:
        return Rendered("")

    full, complete = _take(fold(cleaned(reader.lines())), Budget(max_chars, max_lines), style)
    if complete and not meta.dropped_lines:
        cut = any(len(e.text) > style.max_line for e, _ in full)
        return Rendered(
            "\n".join(text for _, text in full),
            truncated=cut,
            shown=[(1, total)],
            shown_lines=total,
            long_lines_cut=cut,
        )

    available_chars = max(max_chars - MARKER_RESERVE, 200)
    available_lines = max(max_lines - 1, 2)
    head_budget = Budget(
        int(available_chars * HEAD_SHARE), max(int(available_lines * HEAD_SHARE), 1)
    )
    dropped = reader.dropped_range()
    head_end = dropped[0] - 1 if dropped is not None else total
    head, _ = _take(fold(cleaned(reader.lines(1, head_end))), head_budget, style)
    head_last = head[-1][0].last_line_no if head else 0

    tail_budget = Budget(available_chars - head_budget.chars, available_lines - head_budget.lines)
    tail_rev, _ = _take(
        fold(cleaned(reader.lines_reversed(total, head_last + 1)), reverse=True), tail_budget, style
    )
    tail = list(reversed(tail_rev))
    tail_first = tail[0][0].line_no if tail else total + 1

    omitted = tail_first - head_last - 1
    shown_bytes = sum(len(text.encode("utf-8", "surrogatepass")) + 1 for _, text in head + tail)
    omitted_bytes = max(meta.total_text_bytes - shown_bytes, 0)
    parts = [text for _, text in head]
    if omitted > 0:
        parts.append(omission_marker(reader, head_last + 1, tail_first - 1, omitted_bytes))
    parts.extend(text for _, text in tail)
    shown = [s for s in (_span(head), _span(tail)) if s is not None]
    return Rendered(
        "\n".join(parts),
        truncated=True,
        shown=shown,
        shown_lines=sum(b - a + 1 for a, b in shown),
        omitted_lines=omitted,
        omitted_bytes=omitted_bytes,
    )


def omission_marker(reader: SpoolView, first: int, last: int, omitted_bytes: int) -> str:
    handle = reader.handle
    count = last - first + 1
    text = (
        f"[... {count:,} lines omitted (~{human_bytes(omitted_bytes)}) -- "
        f'output(handle="{handle}", offset={first}, limit=200) or '
        f'output(handle="{handle}", grep="<regex>") ...]'
    )
    dropped = reader.dropped_range()
    if dropped is not None and dropped[0] <= last and dropped[1] >= first:
        text += (
            f"\n[... lines {dropped[0]:,}-{dropped[1]:,} "
            f"({human_bytes(reader.meta.dropped_raw_bytes)}) exceeded the spool cap "
            "and were dropped; they cannot be retrieved ...]"
        )
    return text
