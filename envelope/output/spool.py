"""On-disk spool of complete command output, addressable by ``out_<8 hex>`` handles.

Layout of ``<spool_dir>/out_xxxxxxxx/``:

* ``frames-K.bin`` -- raw chunks exactly as read, framed as
  ``<stream u8><seq u32><t f64><len u32><bytes>`` (the authoritative raw record).
* ``lines-K.bin``  -- decoded (uncleaned) lines as ``<stream u8><len u32><utf-8>``.
* ``index-K.bin``  -- one little-endian u64 offset into ``lines-K.bin`` per line,
  appended as lines complete (the incremental line-offset index).
* ``meta.json``    -- summary; rewritten when the run finishes.

Segment 0 is the head. Once the head holds half of the per-run cap, output
goes to rolling tail segments of cap/8 each; when the tail exceeds half the
cap the oldest tail segment is deleted and its bytes/lines are counted as
dropped. Lines are numbered 1..N across the whole output in completion order
(stdout and stderr interleaved), and keep their numbers after drops.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import struct
import time
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, cast

from envelope.log import logger
from envelope.output.decode import StreamDecoder

STDOUT = 1
STDERR = 2
STREAM_NAMES = {STDOUT: "stdout", STDERR: "stderr"}

_FRAME = struct.Struct("<BIdI")
_LINE = struct.Struct("<BI")
_INDEX = struct.Struct("<Q")
HANDLE_RE = re.compile(r"^out_[0-9a-f]{8}$")
TAIL_SEGMENTS = 4


class HandleError(Exception):
    """Unknown or expired output handle."""


@dataclass
class Segment:
    number: int
    first_line: int
    lines: int = 0
    raw_bytes: int = 0
    text_bytes: int = 0


@dataclass
class StreamSummary:
    bytes: int = 0
    lines: int = 0
    encoding: str | None = None
    binary: bool = False
    binary_type: str | None = None
    replaced: int = 0


@dataclass
class SpoolMeta:
    handle: str
    created: float
    info: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    complete: bool = False
    total_lines: int = 0
    total_raw_bytes: int = 0
    total_text_bytes: int = 0
    dropped_raw_bytes: int = 0
    dropped_lines: int = 0
    segments: list[Segment] = field(default_factory=lambda: list[Segment]())
    streams: dict[str, StreamSummary] = field(
        default_factory=lambda: {"stdout": StreamSummary(), "stderr": StreamSummary()}
    )
    ends_without_newline: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> SpoolMeta:
        meta = SpoolMeta(handle=data["handle"], created=data["created"])
        for key in (
            "info",
            "complete",
            "total_lines",
            "total_raw_bytes",
            "total_text_bytes",
            "dropped_raw_bytes",
            "dropped_lines",
            "ends_without_newline",
        ):
            if key in data:
                setattr(meta, key, data[key])
        meta.segments = [Segment(**s) for s in data.get("segments", [])]
        meta.streams = {k: StreamSummary(**v) for k, v in data.get("streams", {}).items()}
        return meta


class SpoolWriter:
    """Receives raw chunks in arrival order; not thread-safe (use from one thread)."""

    def __init__(
        self,
        directory: Path,
        meta: SpoolMeta,
        *,
        cap_bytes: int,
        fallback_codepage: str | None,
    ) -> None:
        self.dir = directory
        self.meta = meta
        self.cap = cap_bytes
        self._head_cap = max(cap_bytes // 2, 1)
        self._tail_seg_cap = max(cap_bytes // (2 * TAIL_SEGMENTS), 1)
        self._decoders = {
            STDOUT: StreamDecoder(fallback_codepage),
            STDERR: StreamDecoder(fallback_codepage),
        }
        self._partial: dict[int, list[str]] = {STDOUT: [], STDERR: []}
        self._seq = 0
        self._start = time.monotonic()
        self._files: dict[str, BinaryIO] = {}
        self._segment = Segment(0, 1)
        self.meta.segments.append(self._segment)
        self._open_segment()
        self.last_line = ""

    # --- files -----------------------------------------------------------------

    def _open_segment(self) -> None:
        for handle in self._files.values():
            handle.close()
        n = self._segment.number
        self._files = {
            "frames": open(self.dir / f"frames-{n}.bin", "ab"),  # noqa: SIM115
            "lines": open(self.dir / f"lines-{n}.bin", "ab"),  # noqa: SIM115
            "index": open(self.dir / f"index-{n}.bin", "ab"),  # noqa: SIM115
        }
        self._line_offset = 0

    def _roll_if_needed(self) -> None:
        seg = self._segment
        limit = self._head_cap if seg.number == 0 else self._tail_seg_cap
        if seg.raw_bytes < limit:
            return
        self._segment = Segment(seg.number + 1, self.meta.total_lines + 1)
        self.meta.segments.append(self._segment)
        self._open_segment()
        tails = [s for s in self.meta.segments if s.number > 0]
        while len(tails) > TAIL_SEGMENTS:
            victim = tails.pop(0)
            self.meta.segments.remove(victim)
            self.meta.dropped_raw_bytes += victim.raw_bytes
            self.meta.dropped_lines += victim.lines
            for kind in ("frames", "lines", "index"):
                try:
                    (self.dir / f"{kind}-{victim.number}.bin").unlink()
                except OSError:
                    pass

    # --- input -------------------------------------------------------------------

    def add(self, stream: int, data: bytes) -> None:
        if not data:
            return
        self._roll_if_needed()
        self._seq += 1
        frames = self._files["frames"]
        frames.write(_FRAME.pack(stream, self._seq, time.monotonic() - self._start, len(data)))
        frames.write(data)
        self._segment.raw_bytes += len(data)
        self.meta.total_raw_bytes += len(data)
        self.meta.streams[STREAM_NAMES[stream]].bytes += len(data)
        self._text(stream, self._decoders[stream].feed(data), final=False)

    def _text(self, stream: int, text: str, *, final: bool) -> None:
        if text:
            parts = text.split("\n")
            partial = self._partial[stream]
            if len(parts) > 1:
                self._emit(stream, "".join(partial) + parts[0])
                partial.clear()
                for line in parts[1:-1]:
                    self._emit(stream, line)
            if parts[-1]:
                partial.append(parts[-1])
                self.last_line = "".join(partial[-4:])[-200:]
        if final and self._partial[stream]:
            self._emit(stream, "".join(self._partial[stream]))
            self._partial[stream].clear()
            self.meta.ends_without_newline = True

    def _emit(self, stream: int, line: str) -> None:
        encoded = line.encode("utf-8", "surrogatepass")
        record = _LINE.pack(stream, len(encoded)) + encoded
        self._files["index"].write(_INDEX.pack(self._line_offset))
        self._files["lines"].write(record)
        self._line_offset += len(record)
        self._segment.lines += 1
        self._segment.text_bytes += len(encoded) + 1
        self.meta.total_lines += 1
        self.meta.total_text_bytes += len(encoded) + 1
        self.meta.streams[STREAM_NAMES[stream]].lines += 1
        self.meta.ends_without_newline = False
        if line.strip():
            self.last_line = line[-200:]

    def set_fallback_codepage(self, codepage: str | None) -> None:
        """Set the console code page for non-UTF-8 output (before any data arrives)."""
        for decoder in self._decoders.values():
            decoder.fallback = codepage

    def flush(self) -> None:
        for handle in self._files.values():
            handle.flush()

    def finish(self) -> SpoolMeta:
        for stream, decoder in self._decoders.items():
            self._text(stream, decoder.finish(), final=True)
            summary = self.meta.streams[STREAM_NAMES[stream]]
            summary.encoding = decoder.encoding
            summary.binary = decoder.binary
            summary.binary_type = decoder.binary_type
            summary.replaced = decoder.replaced
        for handle in self._files.values():
            handle.close()
        self._files = {}
        self.meta.complete = True
        _write_meta(self.dir, self.meta)
        return self.meta

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


def _write_meta(directory: Path, meta: SpoolMeta) -> None:
    tmp = directory / "meta.json.tmp"
    tmp.write_text(json.dumps(meta.to_json()), encoding="utf-8")
    os.replace(tmp, directory / "meta.json")


class SpoolReader:
    def __init__(self, directory: Path, meta: SpoolMeta) -> None:
        self.dir = directory
        self.meta = meta
        self._starts = [s.first_line for s in meta.segments]

    @property
    def handle(self) -> str:
        return self.meta.handle

    @property
    def total_lines(self) -> int:
        return self.meta.total_lines

    def _segment_for(self, line_no: int) -> Segment | None:
        i = bisect_right(self._starts, line_no) - 1
        if i < 0:
            return None
        seg = self.meta.segments[i]
        return seg if line_no < seg.first_line + seg.lines else None

    def is_dropped(self, line_no: int) -> bool:
        return 1 <= line_no <= self.total_lines and self._segment_for(line_no) is None

    def dropped_range(self) -> tuple[int, int] | None:
        if not self.meta.dropped_lines or len(self.meta.segments) < 2:
            return None
        head = self.meta.segments[0]
        first = head.first_line + head.lines
        return first, self.meta.segments[1].first_line - 1

    def lines(self, start: int = 1, end: int | None = None) -> Iterator[tuple[int, int, str]]:
        """Yield (line number, stream, text) for stored lines in [start, end]."""
        end = self.total_lines if end is None else min(end, self.total_lines)
        start = max(start, 1)
        for seg in self.meta.segments:
            seg_end = seg.first_line + seg.lines - 1
            lo, hi = max(start, seg.first_line), min(end, seg_end)
            if lo > hi:
                continue
            with (
                open(self.dir / f"index-{seg.number}.bin", "rb") as index,
                open(self.dir / f"lines-{seg.number}.bin", "rb") as lines,
            ):
                index.seek((lo - seg.first_line) * _INDEX.size)
                (offset,) = _INDEX.unpack(index.read(_INDEX.size))
                lines.seek(offset)
                for line_no in range(lo, hi + 1):
                    stream, length = _LINE.unpack(lines.read(_LINE.size))
                    yield line_no, stream, lines.read(length).decode("utf-8", "surrogatepass")

    def lines_reversed(
        self, end: int | None = None, start: int = 1
    ) -> Iterator[tuple[int, int, str]]:
        """Yield stored lines from ``end`` down to ``start`` in chunks (for tails)."""
        end = self.total_lines if end is None else end
        chunk = 512
        hi = end
        while hi >= start:
            lo = max(hi - chunk + 1, start)
            yield from reversed(list(self.lines(lo, hi)))
            hi = lo - 1

    def raw_frames(self) -> Iterator[tuple[int, int, float, bytes]]:
        for seg in self.meta.segments:
            path = self.dir / f"frames-{seg.number}.bin"
            with open(path, "rb") as frames:
                while header := frames.read(_FRAME.size):
                    stream, seq, t, length = _FRAME.unpack(header)
                    yield stream, seq, t, frames.read(length)


class SpoolManager:
    def __init__(
        self,
        directory: Path,
        *,
        cap_bytes: int,
        total_bytes: int,
        retention_s: float,
    ) -> None:
        self.dir = directory
        self.cap = cap_bytes
        self.total = total_bytes
        self.retention_s = retention_s
        self._active: set[str] = set()
        self._known: dict[str, SpoolReader] = {}

    # --- lifecycle ------------------------------------------------------------------

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def _entries(self) -> list[tuple[float, str, Path]]:
        entries: list[tuple[float, str, Path]] = []
        try:
            children = list(self.dir.iterdir())
        except OSError:
            return entries
        for child in children:
            if child.is_dir() and HANDLE_RE.match(child.name):
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    continue
                entries.append((mtime, child.name, child))
        entries.sort()
        return entries

    def _remove(self, handle: str, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        self._known.pop(handle, None)
        logger.debug("spool %s removed", handle)

    def cleanup(self, now: float | None = None) -> None:
        """Delete spools past retention, then evict oldest until under the total cap."""
        now = time.time() if now is None else now
        for mtime, handle, path in self._entries():
            if handle not in self._active and now - mtime > self.retention_s:
                self._remove(handle, path)
        self.enforce_total()

    def enforce_total(self) -> None:
        entries = self._entries()
        sizes = {handle: _dir_size(path) for _, handle, path in entries}
        used = sum(sizes.values())
        for _, handle, path in entries:
            if used <= self.total:
                break
            if handle in self._active:
                continue
            self._remove(handle, path)
            used -= sizes[handle]

    # --- runs ---------------------------------------------------------------------------

    def create(self, info: dict[str, Any], *, fallback_codepage: str | None) -> SpoolWriter:
        self.ensure_dir()
        while True:
            handle = "out_" + secrets.token_hex(4)
            path = self.dir / handle
            try:
                path.mkdir()
                break
            except FileExistsError:
                continue
        self._active.add(handle)
        meta = SpoolMeta(handle=handle, created=time.time(), info=info)
        _write_meta(path, meta)
        return SpoolWriter(path, meta, cap_bytes=self.cap, fallback_codepage=fallback_codepage)

    def release(self, writer: SpoolWriter) -> None:
        self._active.discard(writer.meta.handle)
        self._known[writer.meta.handle] = SpoolReader(writer.dir, writer.meta)
        self.enforce_total()

    def open(self, handle: str) -> SpoolReader:
        if not HANDLE_RE.match(handle):
            raise HandleError(
                f"'{handle}' is not an output handle (expected out_ followed by 8 hex digits)"
            )
        path = self.dir / handle
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            self._known.pop(handle, None)
            raise HandleError(f"output handle {handle} is unknown or expired")
        cached = self._known.get(handle)
        if cached is not None:
            return cached
        try:
            data = cast(dict[str, Any], json.loads(meta_path.read_text(encoding="utf-8")))
            meta = SpoolMeta.from_json(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HandleError(f"output handle {handle} is unreadable: {exc}") from None
        if not meta.complete:
            raise HandleError(
                f"output handle {handle} belongs to a run that did not finish writing"
            )
        reader = SpoolReader(path, meta)
        self._known[handle] = reader
        return reader


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for child in path.iterdir():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total
