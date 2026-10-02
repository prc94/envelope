from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from envelope.output.cleanup import clean_line, fold
from envelope.output.decode import StreamDecoder
from envelope.output.render import render_run_output
from envelope.output.spool import (
    STDERR,
    STDOUT,
    HandleError,
    LiveSpoolReader,
    SpoolManager,
    SpoolReader,
    SpoolView,
)
from envelope.output.view import ViewError, grep_view, range_view, tail_view, unread_view

MIB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 8


def manager(tmp_path: Path, cap: int = 64 * MIB, total: int = 1024 * MIB) -> SpoolManager:
    return SpoolManager(tmp_path / "spool", cap_bytes=cap, total_bytes=total, retention_s=24 * 3600)


def spool(
    tmp_path: Path,
    chunks: list[tuple[int, bytes]],
    *,
    cap: int = 64 * MIB,
    codepage: str | None = None,
) -> SpoolView:
    mgr = manager(tmp_path, cap)
    writer = mgr.create({"command": "test"}, fallback_codepage=codepage)
    for stream, data in chunks:
        writer.add(stream, data)
    writer.finish()
    mgr.release(writer)
    return mgr.open(writer.meta.handle)


def text_of(reader: SpoolView) -> list[tuple[int, str]]:
    return [(stream, text) for _, stream, text in reader.lines()]


# --- decoding -----------------------------------------------------------------------


def decode_all(
    data: bytes, codepage: str | None = None, chunk: int = 7
) -> tuple[str, StreamDecoder]:
    decoder = StreamDecoder(codepage)
    out = "".join(decoder.feed(data[i : i + chunk]) for i in range(0, len(data), chunk))
    return out + decoder.finish(), decoder


def test_utf8_default() -> None:
    text, decoder = decode_all("héllo wörld ✓\n".encode())
    assert text == "héllo wörld ✓\n" and decoder.encoding == "utf-8" and decoder.replaced == 0


def test_utf16_with_bom() -> None:
    text, decoder = decode_all("﻿Größe 42\r\nzweite Zeile\r\n".encode("utf-16-le"))
    assert text == "Größe 42\r\nzweite Zeile\r\n"
    assert decoder.encoding == "utf-16"


def test_utf16_without_bom_pattern() -> None:
    text, decoder = decode_all("Image Name   PID\r\nsvchost.exe  1234\r\n".encode("utf-16-le"))
    assert text.startswith("Image Name") and decoder.encoding == "utf-16-le"


def test_cp850_for_console_shells() -> None:
    raw = "Größe\r\n".encode("cp850")
    text, decoder = decode_all(raw, codepage="cp850")
    assert text == "Größe\r\n" and decoder.encoding == "cp850" and decoder.replaced == 0


def test_invalid_utf8_is_replaced_and_counted() -> None:
    text, decoder = decode_all(b"ok \xff\xfe bad\n")
    assert text == "ok �� bad\n" and decoder.replaced == 2


def test_binary_png_detected() -> None:
    text, decoder = decode_all(PNG, chunk=100)
    assert text == "" and decoder.binary and decoder.binary_type == "PNG"


def test_decoder_decides_on_200ms_idle_without_newline_or_1024_bytes() -> None:
    decoder = StreamDecoder()
    assert decoder.feed(b"Password: ", now=0.0) == ""
    assert decoder.encoding is None  # too short, no newline: not decided yet
    assert decoder.maybe_decide_idle(0.1) == ""  # under the 200ms threshold
    assert decoder.encoding is None
    text = decoder.maybe_decide_idle(0.2)  # exactly at the threshold
    assert text == "Password: " and decoder.encoding == "utf-8"
    assert decoder.maybe_decide_idle(1.0) == ""  # already decided: a no-op


# --- real Windows samples (spike S6, captured 2026-09-25 from a real Windows 10 19044 VM;
# see tests/fixtures/windows_encoding/README.md for exact provenance) ------------------------

FIXTURES = Path(__file__).parent.parent / "fixtures" / "windows_encoding"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_real_cmd_output_is_console_codepage() -> None:
    # cmd with and without chcp 65001 mid-script produce identical bytes (the OS-level reason
    # launch.py never emits a single cmd invocation with chcp before the payload; see D14/S1).
    raw = fixture("cmd_oem_no_chcp.bin")
    assert raw == fixture("cmd_utf8_chcp65001.bin")
    text, decoder = decode_all(raw, codepage="cp437")
    assert text == "Größe 42 \r\nzweite Zeile\r\n"
    assert decoder.encoding == "cp437" and decoder.replaced == 0


def test_real_powershell51_pipe_output_is_console_codepage() -> None:
    text, decoder = decode_all(fixture("powershell51_pipe_default.bin"), codepage="cp437")
    assert text == "Größe 42\r\nzweite Zeile\r\n"
    assert decoder.encoding == "cp437" and decoder.replaced == 0


def test_real_pwsh7_pipe_output_is_also_console_codepage() -> None:
    # pwsh 7 defaults to the OEM code page on a redirected pipe too, absent the chcp 65001
    # trick launch.py always uses (D14) -- this is why that trick is needed for pwsh, not
    # just for legacy Windows PowerShell 5.1.
    text, decoder = decode_all(fixture("pwsh7_pipe_default.bin"), codepage="cp437")
    assert text == "Größe 42\r\n"
    assert decoder.encoding == "cp437"


def test_real_powershell_outfile_unicode_is_utf16_bom() -> None:
    text, decoder = decode_all(fixture("powershell51_outfile_unicode.bin"))
    assert text == "Größe 42\r\nzweite Zeile\r\n"
    assert decoder.encoding == "utf-16"


def test_real_reg_export_is_utf16_bom() -> None:
    text, decoder = decode_all(fixture("reg_export_utf16.bin"))
    assert text.startswith("Windows Registry Editor")
    assert decoder.encoding == "utf-16"


# --- cleanup ----------------------------------------------------------------------------


def test_progress_bar_keeps_final_state() -> None:
    raw = "".join(f"\rDownloading [{'#' * (i // 25):<20}] {i / 5:.0f}%" for i in range(500))
    raw += "\rDownloading [####################] 100%"
    assert clean_line(raw) == "Downloading [####################] 100%"


def test_ansi_backspace_and_erase() -> None:
    assert clean_line("\x1b[1;31merror\x1b[0m: failed\r") == "error: failed"
    assert clean_line("abc\x08\x08XY") == "aXY"
    assert clean_line("50%\r\x1b[Kdone") == "done"
    assert clean_line("\x1b]0;title\x07prompt") == "prompt"


def test_repeat_folding() -> None:
    lines = (
        [(1, STDOUT, "start")]
        + [(i, STDERR, "warning: x") for i in range(2, 314)]
        + [(314, STDOUT, "end")]
    )
    entries = list(fold(lines))
    assert [(e.text, e.count) for e in entries] == [("start", 1), ("warning: x", 312), ("end", 1)]
    reverse = list(fold(reversed(lines), reverse=True))
    assert [(e.line_no, e.count) for e in reverse] == [(314, 1), (2, 312), (1, 1)]
    two = list(fold([(1, 1, "a"), (2, 1, "a"), (3, 1, "b")]))
    assert [e.count for e in two] == [1, 1, 1]


def test_rendered_repeats_and_raw_spool(tmp_path: Path) -> None:
    data = b"\x1b[33mwarning: deprecated\x1b[0m\n" * 312
    reader = spool(tmp_path, [(STDOUT, data)])
    rendered = render_run_output(reader, 12_000, 400)
    assert rendered.body == "warning: deprecated  [line repeated 312 times]"
    assert b"".join(chunk for _, _, _, chunk in reader.raw_frames()) == data


# --- spool ------------------------------------------------------------------------------


def test_interleaving_order(tmp_path: Path) -> None:
    reader = spool(tmp_path, [(STDOUT, b"a\n"), (STDERR, b"b\n"), (STDOUT, b"c\n")])
    assert text_of(reader) == [(STDOUT, "a"), (STDERR, "b"), (STDOUT, "c")]
    assert render_run_output(reader, 12_000, 400).body == "a\n[stderr] b\nc"
    assert [s for s, *_ in reader.raw_frames()] == [STDOUT, STDERR, STDOUT]


def test_index_lookup_matches_full_read(tmp_path: Path) -> None:
    lines = [f"line {i} " + "x" * (i % 37) for i in range(1, 5001)]
    payload = ("\n".join(lines) + "\n").encode()
    chunks = [
        (STDOUT if i % 3 else STDERR, payload[i : i + 997]) for i in range(0, len(payload), 997)
    ]
    reader = spool(tmp_path, chunks)
    full = list(reader.lines())
    assert len(full) == reader.total_lines
    for n in (1, 2, 1234, 4999, reader.total_lines):
        assert list(reader.lines(n, n)) == [full[n - 1]]
    assert list(reader.lines(100, 150)) == full[99:150]
    assert list(reader.lines_reversed(4000, 3990)) == list(reversed(full[3989:4000]))


def test_live_reader_reads_head_tail_range_and_grep_while_writer_appends(
    tmp_path: Path,
) -> None:
    mgr = manager(tmp_path)
    writer = mgr.create({"command": "live"}, fallback_codepage=None)
    handle = writer.meta.handle
    total = 2000

    stop_writing = threading.Event()
    errors: list[BaseException] = []

    def write_loop() -> None:
        try:
            for i in range(total):
                writer.add(STDOUT, f"line {i}\n".encode())
        finally:
            stop_writing.set()

    def read_loop() -> None:
        try:
            while not stop_writing.is_set():
                reader = mgr.open(handle)
                assert isinstance(reader, LiveSpoolReader)
                list(reader.lines(1, 5))  # head
                list(reader.lines_reversed())  # tail
                list(reader.lines(100, 120))  # a range
                view = grep_view(reader, "line 4", 0, "both", 12_000, 400)
                assert "matching line" in view.text  # "1 matching line" or "N matching lines"
        except BaseException as exc:
            errors.append(exc)

    writer_thread = threading.Thread(target=write_loop)
    reader_thread = threading.Thread(target=read_loop)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=30)
    reader_thread.join(timeout=30)
    assert not writer_thread.is_alive() and not reader_thread.is_alive()
    assert not errors

    writer.finish()
    mgr.release(writer)
    final = mgr.open(handle)
    assert isinstance(final, SpoolReader)
    assert final.total_lines == total


def test_live_reader_pin_defers_unlink_while_writer_rolls_many_segments(tmp_path: Path) -> None:
    mgr = manager(tmp_path, cap=800)  # tiny cap: a handful of writes roll several tail segments
    writer = mgr.create({"command": "live"}, fallback_codepage=None)
    handle = writer.meta.handle
    line = b"x" * 50 + b"\n"
    for _ in range(20):
        writer.add(STDOUT, line)
        if any(s.number == 1 for s in writer.meta.segments):
            break
    else:
        pytest.fail("expected segment 1 to exist by now")

    reader = mgr.open(handle)
    assert isinstance(reader, LiveSpoolReader)
    gen = reader.lines_reversed()
    next(gen)  # starts the generator body, pinning the segments in its snapshot (incl. 1)

    seg1_files = [writer.dir / f"{kind}-1.bin" for kind in ("frames", "lines", "index")]
    assert all(p.exists() for p in seg1_files)

    stop = threading.Event()

    def roll_more() -> None:
        try:
            for _ in range(400):
                writer.add(STDOUT, line)
        finally:
            stop.set()

    writer_thread = threading.Thread(target=roll_more)
    writer_thread.start()
    writer_thread.join(timeout=30)
    assert stop.is_set()

    # segment 1 has been rolled past TAIL_SEGMENTS and logically dropped...
    assert not any(s.number == 1 for s in writer.snapshot.segments)
    # ...but its files must still exist: the still-open generator pins it
    assert all(p.exists() for p in seg1_files)

    list(gen)  # drain the generator -> unpins

    # now that nothing pins it, the deferred unlink has happened
    assert not any(p.exists() for p in seg1_files)


def test_live_reader_degrades_to_dropped_when_a_segment_file_is_missing_under_it(
    tmp_path: Path,
) -> None:
    mgr = manager(tmp_path)
    writer = mgr.create({"command": "live"}, fallback_codepage=None)
    handle = writer.meta.handle
    writer.add(STDOUT, b"a\n")
    writer.add(STDOUT, b"b\n")
    reader = mgr.open(handle)
    assert isinstance(reader, LiveSpoolReader)

    # simulate a foreign deletion race that pinning alone cannot close (e.g. a
    # segment vanishing between taking the snapshot and pinning it)
    for kind in ("frames", "lines", "index"):
        (writer.dir / f"{kind}-0.bin").unlink()

    assert list(reader.lines()) == []
    assert list(reader.raw_frames()) == []


def test_snapshot_exposes_a_prompt_without_a_newline(tmp_path: Path) -> None:
    writer = manager(tmp_path).create({"command": "live"}, fallback_codepage=None)
    writer.add(STDOUT, b"Password: ")
    assert writer.snapshot.unterminated[STDOUT] == "Password: "
    assert writer.meta.total_lines == 0  # not a complete line yet


def test_snapshot_resolves_carriage_return_progress_line(tmp_path: Path) -> None:
    writer = manager(tmp_path).create({"command": "live"}, fallback_codepage=None)
    writer.add(STDOUT, b"0%\r50%\r100%")
    assert writer.snapshot.unterminated[STDOUT] == "100%"
    assert writer.meta.total_lines == 0


def test_unterminated_line_becomes_a_complete_line_once_its_newline_arrives(
    tmp_path: Path,
) -> None:
    writer = manager(tmp_path).create({"command": "live"}, fallback_codepage=None)
    writer.add(STDOUT, b"building")
    assert writer.snapshot.unterminated[STDOUT] == "building"
    assert writer.meta.total_lines == 0

    writer.add(STDOUT, b"... done\n")
    assert writer.snapshot.unterminated[STDOUT] == ""
    assert writer.meta.total_lines == 1
    assert next(SpoolReader(writer.dir, writer.meta).lines()) == (1, STDOUT, "building... done")


def test_cap_keeps_head_and_tail(tmp_path: Path) -> None:
    cap = 1 * MIB
    line = b"%08d " + b"y" * 90 + b"\n"
    chunks = [(STDOUT, b"".join(line % (i * 100 + j) for j in range(100))) for i in range(300)]
    total = sum(len(c) for _, c in chunks)  # ~3 MiB against a 1 MiB cap
    reader = spool(tmp_path, chunks, cap=cap)
    meta = reader.meta
    assert meta.dropped_raw_bytes > 0
    stored = sum(len(c) for _, _, _, c in reader.raw_frames())
    assert stored + meta.dropped_raw_bytes == total
    assert stored <= cap + cap // 8
    first = next(reader.lines())
    assert first[2].startswith("00000000")
    last = next(iter(reader.lines_reversed()))
    assert last[2].startswith(f"{300 * 100 - 1:08d}")
    body = render_run_output(reader, 12_000, 400).body
    assert "were dropped" in body and "cannot be retrieved" in body


@pytest.mark.slow
def test_cap_200mib_output_64mib_cap(tmp_path: Path) -> None:
    chunk = (b"z" * 1023 + b"\n") * 1024  # 1 MiB
    mgr = manager(tmp_path)
    writer = mgr.create({}, fallback_codepage=None)
    for _ in range(200):
        writer.add(STDOUT, chunk)
    meta = writer.finish()
    mgr.release(writer)
    assert meta.total_raw_bytes == 200 * MIB
    kept = meta.total_raw_bytes - meta.dropped_raw_bytes
    assert 32 * MIB <= kept <= 64 * MIB + 8 * MIB
    assert "dropped" in render_run_output(mgr.open(meta.handle), 12_000, 400).body


def test_handles_eviction_and_retention(tmp_path: Path) -> None:
    mgr = manager(tmp_path, total=300_000)
    handles: list[str] = []
    for i in range(4):
        writer = mgr.create({}, fallback_codepage=None)
        writer.add(STDOUT, os.urandom(50_000).hex().encode())  # ~100 KB of text each
        writer.finish()
        mgr.release(writer)
        handles.append(writer.meta.handle)
        os.utime(writer.dir, (time.time() - 100 + i, time.time() - 100 + i))
        assert re.fullmatch(r"out_[0-9a-f]{8}", writer.meta.handle)
    mgr.enforce_total()
    with pytest.raises(HandleError, match="unknown or expired"):
        mgr.open(handles[0])
    assert mgr.open(handles[-1]).total_lines == 1
    old = mgr.dir / handles[-1]
    os.utime(old, (time.time() - 25 * 3600, time.time() - 25 * 3600))
    mgr.cleanup()
    with pytest.raises(HandleError):
        mgr.open(handles[-1])
    with pytest.raises(HandleError, match="not an output handle"):
        mgr.open("../../etc")


def test_touch_refreshes_the_spool_directory_mtime(tmp_path: Path) -> None:
    writer = manager(tmp_path).create({}, fallback_codepage=None)
    os.utime(writer.dir, (time.time() - 1000, time.time() - 1000))
    before = writer.dir.stat().st_mtime
    writer.touch()
    assert writer.dir.stat().st_mtime > before


def test_second_manager_cleanup_protects_a_live_owner(tmp_path: Path) -> None:
    mgr1 = manager(tmp_path, total=300_000)
    writer = mgr1.create({}, fallback_codepage=None)
    writer.add(STDOUT, b"still running\n")  # never finished/released: stays live
    assert (writer.dir / "owner.json").exists()
    os.utime(writer.dir, (time.time() - 25 * 3600, time.time() - 25 * 3600))  # looks ancient

    mgr2 = manager(tmp_path, total=300_000)  # a second instance, same directory
    mgr2.cleanup()  # our own PID (the recorded owner) is genuinely still running

    assert writer.dir.exists(), "a live owner must protect the spool across instances"


def test_second_manager_cleanup_removes_a_stale_owner(tmp_path: Path) -> None:
    mgr1 = manager(tmp_path, total=300_000)
    writer = mgr1.create({}, fallback_codepage=None)
    writer.add(STDOUT, b"orphaned\n")

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (writer.dir / "owner.json").write_text(json.dumps({"pid": dead.pid, "start": None}))
    os.utime(writer.dir, (time.time() - 25 * 3600, time.time() - 25 * 3600))

    mgr2 = manager(tmp_path, total=300_000)
    mgr2.cleanup()

    assert not writer.dir.exists(), "a dead owner must not protect the spool"


def test_enforce_total_counts_but_never_evicts_live_spools(tmp_path: Path) -> None:
    mgr = manager(tmp_path, total=80_000)
    live = [mgr.create({}, fallback_codepage=None) for _ in range(3)]
    for w in live:
        w.add(STDOUT, os.urandom(20_000).hex().encode())  # ~40 KB each: 120 KB > 80 KB cap

    finished = mgr.create({}, fallback_codepage=None)
    finished.add(STDOUT, os.urandom(5_000).hex().encode())
    finished.finish()
    mgr.release(finished)  # no longer live; the only evictable entry

    mgr.enforce_total()

    for w in live:
        assert w.dir.exists(), "a live (still-writing) spool must never be evicted"
    assert not finished.dir.exists(), "over cap, with only one evictable entry available"


# --- truncation ---------------------------------------------------------------------------


def test_small_output_not_truncated(tmp_path: Path) -> None:
    reader = spool(tmp_path, [(STDOUT, "".join(f"line {i}\n" for i in range(1, 21)).encode())])
    rendered = render_run_output(reader, 12_000, 400)
    assert rendered.body.splitlines() == [f"line {i}" for i in range(1, 21)]
    assert not rendered.truncated and "omitted" not in rendered.body


def test_large_output_head_tail_marker(tmp_path: Path) -> None:
    reader = spool(
        tmp_path, [(STDOUT, "".join(f"test {i} ok\n" for i in range(1, 18_001)).encode())]
    )
    rendered = render_run_output(reader, 12_000, 400)
    body = rendered.body
    assert len(body) <= 12_000 and len(body.splitlines()) <= 400
    lines = body.splitlines()
    assert lines[0] == "test 1 ok" and lines[-1] == "test 18000 ok"
    marker = next(line for line in lines if line.startswith("[... "))
    match = re.search(r"\[\.\.\. ([\d,]+) lines omitted .*offset=(\d+), limit=200\)", marker)
    assert match is not None
    omitted, offset = int(match.group(1).replace(",", "")), int(match.group(2))
    head = lines.index(marker)
    assert offset == head + 1
    assert f'output(handle="{reader.handle}", grep=' in marker
    tail = len(lines) - head - 1
    assert tail > head  # favors the tail
    assert omitted == 18_000 - head - tail == rendered.omitted_lines


# --- output tool views ----------------------------------------------------------------------


def make_log(tmp_path: Path) -> SpoolView:
    text = "".join(
        f"FAIL test_{i}\n" if i in (100, 500, 900) else f"PASS test_{i}\n" for i in range(1, 2001)
    )
    return spool(tmp_path, [(STDOUT, text.encode())])


def test_view_grep_with_context(tmp_path: Path) -> None:
    view = grep_view(make_log(tmp_path), "FAIL", 2, "both", 12_000, 400)
    lines = view.text.splitlines()
    assert "3 matching lines" in lines[0] and "2,000 lines total" in lines[0]
    assert (
        "100: FAIL test_100" in lines
        and "98- PASS test_98" in lines
        and "102- PASS test_102" in lines
    )
    assert lines.count("--") == 2
    assert view.structured["matches"] == 3 and view.structured["more_matches"] is False


def test_view_range(tmp_path: Path) -> None:
    view = range_view(make_log(tmp_path), 1000, 50, "both", 12_000, 400)
    lines = view.text.splitlines()
    assert "lines 1,000-1,049" in lines[0] and "2,000 lines total" in lines[0]
    assert lines[1] == "1000: PASS test_1000" and lines[-1] == "1049: PASS test_1049"


def test_view_tail_default_and_streams(tmp_path: Path) -> None:
    reader = spool(tmp_path, [(STDOUT, b"out1\n"), (STDERR, b"err1\n"), (STDOUT, b"out2\n")])
    assert tail_view(reader, 100, "both", 12_000, 400).text.splitlines()[1:] == [
        "1: out1",
        "2: [stderr] err1",
        "3: out2",
    ]
    assert tail_view(reader, 100, "stderr", 12_000, 400).text.splitlines()[1:] == ["2: err1"]


def test_view_grep_budget_reports_more(tmp_path: Path) -> None:
    reader = spool(tmp_path, [(STDOUT, "".join(f"ERROR {i}\n" for i in range(5000)).encode())])
    view = grep_view(reader, "ERROR", 0, "both", 2_000, 400)
    assert view.structured["more_matches"] is True
    assert "more matches beyond the output budget" in view.text
    assert len(view.text) <= 2_400


def test_view_invalid_regex(tmp_path: Path) -> None:
    with pytest.raises(ViewError, match="invalid regular expression"):
        grep_view(make_log(tmp_path), "([", 0, "both", 12_000, 400)


# --- unread ("follow-along") reads and read positions ----------------------------------------


def test_follow_along_reads_only_return_lines_not_shown_before(tmp_path: Path) -> None:
    mgr = manager(tmp_path)
    writer = mgr.create({}, fallback_codepage=None)
    for i in range(1, 11):
        writer.add(STDOUT, f"line {i}\n".encode())
    writer.finish()
    mgr.release(writer)
    reader = mgr.open(writer.meta.handle)

    first = unread_view(reader, mgr.read_position(writer.meta.handle), "both", 12_000, 400)
    assert first.structured["mode"] == "unread"
    assert first.text.splitlines()[1:] == [f"{i}: line {i}" for i in range(1, 11)]
    mgr.advance_read_position(writer.meta.handle, first.structured["last_line"])

    # the run already finished with nothing more written: a second follow-along
    # read at the advanced position finds nothing new
    second = unread_view(reader, mgr.read_position(writer.meta.handle), "both", 12_000, 400)
    assert second.structured.get("first_line") is None
    assert "no lines" in second.text.splitlines()[0]


def test_follow_along_reads_advance_as_a_live_process_grows(tmp_path: Path) -> None:
    mgr = manager(tmp_path)
    writer = mgr.create({}, fallback_codepage=None)
    handle = writer.meta.handle
    for i in range(1, 6):
        writer.add(STDOUT, f"line {i}\n".encode())
    reader = mgr.open(handle)  # a LiveSpoolReader: the run is still going

    first = unread_view(reader, mgr.read_position(handle), "both", 12_000, 400)
    assert first.text.splitlines()[1:] == [f"{i}: line {i}" for i in range(1, 6)]
    mgr.advance_read_position(handle, first.structured["last_line"])

    for i in range(6, 9):
        writer.add(STDOUT, f"line {i}\n".encode())

    second = unread_view(reader, mgr.read_position(handle), "both", 12_000, 400)
    assert second.text.splitlines()[1:] == [f"{i}: line {i}" for i in range(6, 9)]
    assert second.structured["first_line"] == 6 and second.structured["last_line"] == 8


def test_budgeted_unread_result_reports_a_correct_continuation(tmp_path: Path) -> None:
    mgr = manager(tmp_path)
    writer = mgr.create({}, fallback_codepage=None)
    for i in range(1, 501):
        writer.add(STDOUT, f"line {i:04d} padding padding padding\n".encode())
    writer.finish()
    mgr.release(writer)
    reader = mgr.open(writer.meta.handle)

    view = unread_view(reader, 0, "both", 2_000, 400)  # far too small a budget for 500 lines
    assert view.structured["truncated"] is True
    last = view.structured["last_line"]
    assert last < 500
    assert f"offset={last + 1}, limit=" in view.text
    assert "earlier lines via" not in view.text  # forward reading, not the tail-style phrasing


def test_unread_view_reports_a_read_position_inside_a_dropped_range(tmp_path: Path) -> None:
    mgr = manager(tmp_path, cap=800)
    writer = mgr.create({}, fallback_codepage=None)
    for i in range(1, 21):
        writer.add(STDOUT, f"line {i:03d} {'x' * 40}\n".encode())  # distinct lines: no folding
        if writer.meta.dropped_lines > 0:
            break
    else:
        pytest.fail("expected some lines to have been dropped by now")
    writer.finish()
    mgr.release(writer)
    reader = mgr.open(writer.meta.handle)

    dropped = reader.dropped_range()
    assert dropped is not None
    view = unread_view(reader, 0, "both", 12_000, 400)  # position 0: before/inside the drop
    assert f"lines {dropped[0]:,}-{dropped[1]:,} were dropped" in view.text
    first_shown = next(line for line in view.text.splitlines() if line and line[0].isdigit())
    shown_line_no = int(first_shown.split(":", 1)[0])
    assert shown_line_no == dropped[1] + 1


def test_explicit_modes_do_not_move_the_read_position(tmp_path: Path) -> None:
    mgr = manager(tmp_path)
    writer = mgr.create({}, fallback_codepage=None)
    for i in range(1, 21):
        writer.add(STDOUT, f"line {i}\n".encode())
    writer.finish()
    mgr.release(writer)
    handle = writer.meta.handle
    reader = mgr.open(handle)

    assert mgr.read_position(handle) == 0
    tail_view(reader, 5, "both", 12_000, 400)
    range_view(reader, 1, 5, "both", 12_000, 400)
    grep_view(reader, "line", 0, "both", 12_000, 400)
    assert mgr.read_position(handle) == 0  # none of the explicit modes touched it
