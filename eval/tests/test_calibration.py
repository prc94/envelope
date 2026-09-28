"""The calibration comparison, with synthetic captures (the live run is in design E8)."""

import base64

from envelope_eval.calibration import compare, render

INFO = {"session_id": 0, "sessionname_env": None, "username": "envelope", "acp": 1251, "oemcp": 866,
        "console_input_cp": 866, "console_output_cp": 866, "has_console_window": False}  # fmt: skip


def entry(name, out=b"", err=b"", rc=0):
    b64 = lambda data: base64.b64encode(data).decode()
    return {"name": name, "returncode": rc, "stdout": b64(out), "stderr": b64(err)}


def capture(commands, **info):
    return {"info": INFO | info, "commands": commands}


def test_identical_bytes_are_reported_as_identical():
    cyrillic = "отчёт".encode("cp866")
    a = capture([entry("dir", cyrillic), entry("chcp", b"Active code page: 866\r\n")])
    b = capture([entry("dir", cyrillic), entry("chcp", b"Active code page: 866\r\n")], session_id=1)
    result = compare(a, b)
    assert result.same and result.identical == ["dir", "chcp"]
    assert result.info["ssh"]["session_id"] == 0 and result.info["interactive"]["session_id"] == 1
    assert "2 of 2 commands wrote identical bytes" in render(result)


def test_any_byte_or_exit_code_difference_is_listed_with_both_sides():
    a = capture(
        [entry("dir", "отчёт".encode("cp866")), entry("net", b"\\\\\r\n", rc=1), entry("ok", b"x")]
    )
    b = capture(
        [
            entry("dir", "отчёт".encode("cp1251")),
            entry("net", b"\\\\HOST\r\n", rc=0),
            entry("ok", b"x"),
        ]
    )
    result = compare(a, b)
    assert (
        not result.same and result.identical == ["ok"] and set(result.different) == {"dir", "net"}
    )
    assert (
        result.different["net"]["ssh"]["rc"] == 1
        and result.different["net"]["interactive"]["rc"] == 0
    )
    assert (
        result.different["dir"]["ssh"]["stdout"] != result.different["dir"]["interactive"]["stdout"]
    )
    text = render(result)
    assert "1 of 3 commands wrote identical bytes" in text and "DIFFERENT dir" in text


def test_a_command_that_failed_to_start_in_one_context_is_a_difference():
    a = capture([entry("pwsh", b"65001")])
    b = capture([{"name": "pwsh", "error": "FileNotFoundError: pwsh"}])
    assert "pwsh" in compare(a, b).different
    assert compare(b, b).same  # the same failure in both contexts is not a difference between them
