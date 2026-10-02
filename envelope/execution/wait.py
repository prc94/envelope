"""Parsing, validation and coercion for `wait_for` (design B5).

Accepts `exit`, `regex:<pattern>`, `port:<n>`, `idle:<seconds>`, the coerced
spellings `port <n>`, `:<n>`, `localhost:<n>` and `http(s)://localhost:<n>`
(and a bare regular expression, coerced to `regex:<pattern>`), plus the
object forms `{"exit": true}`, `{"regex": "<pattern>"}`, `{"port": <n>}` and
`{"idle": <seconds>}`. Every coercion is recorded; a malformed condition or
an invalid regular expression raises `WaitConditionError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

WaitKind = Literal["exit", "regex", "port", "idle"]


class WaitConditionError(ValueError):
    pass


@dataclass(frozen=True)
class WaitCondition:
    kind: WaitKind
    text: str  # canonical form, e.g. "port:3000" -- used in audit and rendering
    pattern: re.Pattern[str] | None = None
    port: int | None = None
    idle_s: float | None = None


_PORT_BARE = re.compile(r"^port\s+(\d+)$")
_PORT_COLON = re.compile(r"^:(\d+)$")
_PORT_LOCALHOST = re.compile(r"^(?:https?://)?localhost:(\d+)(?:/\S*)?$")
_DIGITS = re.compile(r"^\d+$")


def _port_condition(port_str: str, text: str) -> WaitCondition:
    if not _DIGITS.match(port_str):
        raise WaitConditionError(f"`wait_for` port must be a number (received {port_str!r})")
    port = int(port_str)
    if not (1 <= port <= 65535):
        raise WaitConditionError(f"`wait_for` port {port} is out of range (1-65535)")
    return WaitCondition("port", text, port=port)


def _regex_condition(pattern: str, text: str) -> WaitCondition:
    if not pattern:
        raise WaitConditionError("`wait_for` regex must be a non-empty pattern")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise WaitConditionError(
            f"`wait_for` has an invalid regular expression {pattern!r}: {exc}"
        ) from None
    return WaitCondition("regex", text, pattern=compiled)


def _idle_condition(seconds_str: str, text: str) -> WaitCondition:
    try:
        seconds = float(seconds_str)
    except ValueError:
        raise WaitConditionError(
            f"`wait_for` idle seconds must be a number (received {seconds_str!r})"
        ) from None
    if not (seconds > 0):
        raise WaitConditionError("`wait_for` idle seconds must be a positive number")
    return WaitCondition("idle", text, idle_s=seconds)


def _parse_object(value: dict[Any, Any]) -> tuple[WaitCondition, str | None]:
    keys = list(value.keys())
    if len(keys) != 1:
        raise WaitConditionError(
            "`wait_for` object form must have exactly one of "
            "exit, regex, port or idle (received "
            f"{', '.join(repr(k) for k in keys) or 'nothing'})"
        )
    key = keys[0]
    inner = value[key]
    if key == "exit":
        if inner is not True:
            raise WaitConditionError('`wait_for` {"exit": ...} must be {"exit": true}')
        return WaitCondition("exit", "exit"), "object form to 'exit'"
    if key == "regex":
        if not isinstance(inner, str):
            raise WaitConditionError('`wait_for` {"regex": ...} must be a string pattern')
        condition = _regex_condition(inner, f"regex:{inner}")
        return condition, f"object form to 'regex:{inner}'"
    if key == "port":
        if isinstance(inner, bool) or not isinstance(inner, int | str):
            raise WaitConditionError('`wait_for` {"port": ...} must be a number')
        text = f"port:{inner}"
        condition = _port_condition(str(inner), text)
        return condition, f"object form to {text!r}"
    if key == "idle":
        if isinstance(inner, bool) or not isinstance(inner, int | float | str):
            raise WaitConditionError('`wait_for` {"idle": ...} must be a number of seconds')
        text = f"idle:{inner}"
        condition = _idle_condition(str(inner), text)
        return condition, f"object form to {text!r}"
    raise WaitConditionError(
        f"`wait_for` object form has an unknown key {key!r}; use exit, regex, port or idle"
    )


def parse_wait_for(value: Any) -> tuple[WaitCondition, str | None]:
    """Parse `wait_for` into a `WaitCondition`. Returns ``(condition,
    coercion description or None)``. Raises ``WaitConditionError`` for a
    malformed condition or an invalid regular expression."""
    if isinstance(value, dict):
        return _parse_object(cast("dict[Any, Any]", value))
    if not isinstance(value, str):
        raise WaitConditionError(
            f"`wait_for` must be a string or object (received {type(value).__name__})"
        )
    raw = value.strip()
    if not raw:
        raise WaitConditionError("`wait_for` must not be empty")
    if raw == "exit":
        return WaitCondition("exit", "exit"), None
    if raw.startswith("regex:"):
        return _regex_condition(raw[len("regex:") :], raw), None
    if raw.startswith("port:"):
        return _port_condition(raw[len("port:") :], raw), None
    if raw.startswith("idle:"):
        return _idle_condition(raw[len("idle:") :], raw), None
    for coerced in (_PORT_BARE.match(raw), _PORT_COLON.match(raw), _PORT_LOCALHOST.match(raw)):
        if coerced:
            port = coerced.group(1)
            text = f"port:{port}"
            return _port_condition(port, text), f"{raw!r} to {text!r}"
    # Anything else is a bare regular expression (design B5): coerced by
    # prefixing it with "regex:".
    text = f"regex:{raw}"
    return _regex_condition(raw, text), f"{raw!r} to {text!r}"
