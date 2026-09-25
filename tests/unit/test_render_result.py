"""Snapshot tests for the text result layout."""

from __future__ import annotations

from envelope.output.render import Rendered
from envelope.output.spool import SpoolMeta
from envelope.render.hints import Hint
from envelope.render.result import RunReport, run_structured, run_text


def meta(lines: int = 1, text_bytes: int = 3, **kw: object) -> SpoolMeta:
    m = SpoolMeta(handle="out_91c2ab04", created=0.0)
    m.total_lines = lines
    m.total_text_bytes = text_bytes
    m.streams["stdout"].lines = lines
    m.streams["stdout"].bytes = text_bytes
    for key, value in kw.items():
        setattr(m, key, value)
    return m


def report(**kw: object) -> RunReport:
    base: dict[str, object] = {
        "shell": "bash",
        "cwd": "/home/u",
        "duration_s": 0.012,
        "exit_code": 0,
        "meta": meta(),
        "rendered": Rendered("hi", shown=[(1, 1)], shown_lines=1),
    }
    base.update(kw)
    return RunReport(**base)  # type: ignore[arg-type]


def test_success() -> None:
    assert run_text(report()) == "exit 0 | 0.01s | bash | cwd /home/u\nhi"


def test_failure() -> None:
    text = run_text(
        report(exit_code=2, duration_s=12.34, rendered=Rendered("boom", shown=[(1, 1)]))
    )
    assert text == "exit 2 | 12.3s | bash | cwd /home/u\nboom"


def test_timeout_with_partial_output_and_hint() -> None:
    text = run_text(
        report(
            exit_code=None,
            timed_out=True,
            timeout_s=120,
            duration_s=120.02,
            rendered=Rendered("partial", shown=[(1, 1)]),
            hints=[
                Hint(
                    "timed_out",
                    "note: timed out after 120s; the command and all its child processes were terminated",
                )
            ],
        )
    )
    assert text == (
        "timed out after 120s | 120.0s | bash | cwd /home/u\n"
        "partial\n"
        "note: timed out after 120s; the command and all its child processes were terminated"
    )


def test_cancelled() -> None:
    assert run_text(report(cancelled=True, exit_code=None)).startswith("cancelled | 0.01s | bash")


def test_empty_output() -> None:
    text = run_text(report(meta=meta(0, 0), rendered=Rendered("")))
    assert text == "exit 0 | 0.01s | bash | cwd /home/u\n(no output)"


def test_truncated_line_and_structured_metadata() -> None:
    rendered = Rendered(
        "head\n[... omitted ...]\ntail",
        truncated=True,
        shown=[(1, 40), (17_641, 18_000)],
        shown_lines=400,
    )
    r = report(
        meta=meta(18_000, 1_500_000),
        rendered=rendered,
        decision={"verdict": "allow", "rule": "default"},
    )
    lines = run_text(r).splitlines()
    assert lines[1] == (
        "output: 18,000 lines, 1.4 MB; showing lines 1-40 and 17,641-18,000; handle out_91c2ab04"
    )
    structured = run_structured(r)
    assert structured["handle"] == "out_91c2ab04" and structured["truncated"] is True
    assert "head" not in str(structured) and "tail" not in str(structured)
    assert structured["decision"] == {"verdict": "allow", "rule": "default"}


def test_windows_crash_code_header() -> None:
    assert run_text(report(exit_code=0xC0000005, shell="cmd", cwd="C:\\src")).startswith(
        "exit 0xC0000005 | 0.01s | cmd | cwd C:\\src"
    )


def test_signal_header() -> None:
    assert run_text(report(exit_code=None, signal=9)).startswith("signal 9 (SIGKILL) |")
