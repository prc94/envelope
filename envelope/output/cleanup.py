"""Line cleanup for rendering: ANSI stripping, carriage-return and backspace
resolution, and folding of repeated lines. The spool keeps the raw bytes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

# CSI (ESC [ ... final), OSC (ESC ] ... BEL/ST), other ESC sequences, 8-bit CSI.
_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[ -/]*[0-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
_TOKENS = re.compile(
    r"(?P<csi>\x1b\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~]))"
    r"|(?P<esc>\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[PX^_][^\x1b]*(?:\x1b\\)?|\x1b[ -/]*[0-~]|\x9b[0-?]*[ -/]*[@-~])"
    r"|(?P<cr>\r)|(?P<bs>\x08)"
)
_OTHER_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_line(line: str) -> str:
    """Return what a terminal would finally show for this line (no newline)."""
    if line.endswith("\r"):
        line = line[:-1]
    if "\r" not in line and "\x08" not in line:
        if "\x1b" in line or "\x9b" in line:
            line = _ESCAPE.sub("", line)
        return _OTHER_CONTROLS.sub("", line)
    buf: list[str] = []
    cursor = 0
    pos = 0
    for match in _TOKENS.finditer(line):
        cursor = _write(buf, cursor, line[pos : match.start()])
        pos = match.end()
        if match.group("cr"):
            cursor = 0
        elif match.group("bs"):
            cursor = max(cursor - 1, 0)
        elif match.group("csi"):
            final = match.group("final")
            params = match.group("params")
            if final == "K":
                if params in ("", "0"):
                    del buf[cursor:]
                elif params == "1":
                    buf[:cursor] = [" "] * min(cursor, len(buf))
                elif params == "2":
                    buf.clear()
            elif final == "G":
                cursor = (
                    max(int(params or "1") - 1, 0) if params.isdigit() or not params else cursor
                )
            elif final == "D":
                cursor = (
                    max(cursor - int(params or "1"), 0)
                    if params.isdigit() or not params
                    else cursor
                )
    cursor = _write(buf, cursor, line[pos:])
    return _OTHER_CONTROLS.sub("", "".join(buf).rstrip(" ") if buf else "")


def _write(buf: list[str], cursor: int, text: str) -> int:
    if not text:
        return cursor
    if cursor >= len(buf):
        buf.extend(" " * (cursor - len(buf)))
        buf.extend(text)
        return cursor + len(text)
    end = cursor + len(text)
    buf[cursor:end] = list(text)
    return end


@dataclass
class Entry:
    """A rendered line (or a run of identical lines) with its first line number."""

    line_no: int
    stream: int
    text: str
    count: int = 1

    @property
    def last_line_no(self) -> int:
        return self.line_no + self.count - 1


FOLD_MIN = 3


def fold(lines: Iterable[tuple[int, int, str]], *, reverse: bool = False) -> Iterator[Entry]:
    """Fold runs of >= FOLD_MIN identical consecutive (stream, text) lines.

    With ``reverse=True`` the input is in descending line order (for tails);
    yielded entries are then also in descending order.
    """
    current: Entry | None = None
    step = -1 if reverse else 1

    def expand(entry: Entry) -> Iterator[Entry]:
        if entry.count >= FOLD_MIN:
            yield entry
            return
        numbers = range(entry.count) if not reverse else range(entry.count - 1, -1, -1)
        for i in numbers:
            yield Entry(entry.line_no + i, entry.stream, entry.text)

    edge = 0
    for line_no, stream, text in lines:
        if (
            current is not None
            and current.stream == stream
            and current.text == text
            and edge + step == line_no
        ):
            current.count += 1
            edge = line_no
            if reverse:
                current.line_no = line_no
            continue
        if current is not None:
            yield from expand(current)
        current = Entry(line_no, stream, text)
        edge = line_no
    if current is not None:
        yield from expand(current)
