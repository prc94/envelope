from __future__ import annotations

import pytest

from envelope.execution.wait import WaitConditionError, parse_wait_for


def test_exit() -> None:
    condition, note = parse_wait_for("exit")
    assert condition.kind == "exit"
    assert note is None


@pytest.mark.parametrize(
    "value",
    ["regex:compiled (successfully|with errors)", "regex:^ready$"],
)
def test_explicit_regex(value: str) -> None:
    condition, note = parse_wait_for(value)
    assert condition.kind == "regex"
    assert note is None
    assert condition.pattern is not None
    assert condition.pattern.search("compiled successfully") or "ready" in value


def test_bare_regex_is_coerced() -> None:
    condition, note = parse_wait_for("compiled successfully")
    assert condition.kind == "regex"
    assert condition.pattern is not None
    assert condition.pattern.search("compiled successfully")
    assert note == "'compiled successfully' to 'regex:compiled successfully'"


def test_explicit_port() -> None:
    condition, note = parse_wait_for("port:3000")
    assert condition.kind == "port"
    assert condition.port == 3000
    assert note is None


@pytest.mark.parametrize(
    "spelling",
    ["port 3000", ":3000", "localhost:3000", "http://localhost:3000", "https://localhost:3000/x"],
)
def test_coerced_port_spellings(spelling: str) -> None:
    condition, note = parse_wait_for(spelling)
    assert condition.kind == "port"
    assert condition.port == 3000
    assert note == f"{spelling!r} to 'port:3000'"


def test_explicit_idle() -> None:
    condition, note = parse_wait_for("idle:10")
    assert condition.kind == "idle"
    assert condition.idle_s == 10.0
    assert note is None


def test_object_form_exit() -> None:
    condition, note = parse_wait_for({"exit": True})
    assert condition.kind == "exit"
    assert note == "object form to 'exit'"


def test_object_form_port() -> None:
    condition, note = parse_wait_for({"port": 3000})
    assert condition.kind == "port"
    assert condition.port == 3000
    assert note == "object form to 'port:3000'"


def test_object_form_regex() -> None:
    condition, note = parse_wait_for({"regex": "ready"})
    assert condition.kind == "regex"
    assert condition.pattern is not None
    assert note == "object form to 'regex:ready'"


def test_object_form_idle() -> None:
    condition, _note = parse_wait_for({"idle": 5})
    assert condition.kind == "idle"
    assert condition.idle_s == 5.0


@pytest.mark.parametrize(
    "value",
    [
        "port:abc",
        "port:0",
        "port:70000",
        "idle:abc",
        "idle:0",
        "idle:-5",
        "regex:(unterminated",
        "regex:",
        "",
        123,
        {"port": "abc"},
        {"exit": False},
        {"idle": "not-a-number"},
        {"frobnicate": 1},
        {},
        {"port": 1, "idle": 2},
    ],
)
def test_malformed_conditions_raise(value: object) -> None:
    with pytest.raises(WaitConditionError):
        parse_wait_for(value)
