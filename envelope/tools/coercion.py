"""Lenient argument coercion for open-weight models (design D3).

Accepts numeric strings for numbers, "true"/"false" for booleans, JSON-encoded
strings for objects, and ``KEY=value`` lines for environment maps. Every
coercion is recorded; uncoercible values raise ``ArgumentError`` naming the
parameter, the received type and the expected form.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from envelope.execution.wait import WaitConditionError, parse_wait_for

Kind = Literal["string", "number", "integer", "boolean", "env", "choice", "wait_for"]


@dataclass(frozen=True)
class Param:
    name: str
    kind: Kind
    expected: str
    choices: tuple[str, ...] = ()


class ArgumentError(Exception):
    def __init__(self, param: str, message: str) -> None:
        super().__init__(message)
        self.param = param


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _preview(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 40 else text[:37] + "..."


def _fail(param: Param, value: Any) -> ArgumentError:
    return ArgumentError(
        param.name,
        f"`{param.name}` {param.expected} (received {_type_name(value)} {_preview(value)})",
    )


def _parse_env_lines(text: str) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if not sep or not name or " " in name:
            return None
        result[name] = value
    return result


def _env_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    return None


def coerce_value(param: Param, value: Any) -> tuple[Any, str | None]:
    """Return (value, coercion description or None)."""
    kind = param.kind
    if kind == "string":
        if isinstance(value, str):
            return value, None
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value), "number to string"
        raise _fail(param, value)
    if kind in ("number", "integer"):
        if isinstance(value, bool):
            raise _fail(param, value)
        number: float
        note = None
        if isinstance(value, int | float):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                raise _fail(param, value) from None
            note = "string to number"
        else:
            raise _fail(param, value)
        if not math.isfinite(number):
            raise _fail(param, value)
        if kind == "integer":
            if not number.is_integer():
                raise _fail(param, value)
            return int(number), note
        return (int(number) if number.is_integer() and isinstance(value, int) else number), note
    if kind == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", "string to boolean"
        if isinstance(value, int) and value in (0, 1):
            return bool(value), "number to boolean"
        raise _fail(param, value)
    if kind == "wait_for":
        try:
            return parse_wait_for(value)
        except WaitConditionError as exc:
            raise ArgumentError(param.name, str(exc)) from None
    if kind == "choice":
        if isinstance(value, str):
            lowered = value.strip().lower()
            for choice in param.choices:
                if choice == lowered:
                    return choice, None if choice == value else "normalized case"
        raise _fail(param, value)
    # env
    note = None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}, "empty string to empty object"
        if stripped.startswith("{"):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                raise _fail(param, value) from None
            note = "JSON string to object"
        else:
            parsed = _parse_env_lines(stripped)
            if parsed is None:
                raise _fail(param, value)
            return parsed, "KEY=value lines to object"
    if isinstance(value, list):
        lines = cast(list[Any], value)
        if all(isinstance(item, str) for item in lines):
            parsed = _parse_env_lines("\n".join(cast(list[str], lines)))
            if parsed is not None:
                return parsed, "KEY=value array to object"
        raise _fail(param, value)
    if not isinstance(value, dict):
        raise _fail(param, value)
    result: dict[str, str] = {}
    for key, item in cast(dict[Any, Any], value).items():
        text = _env_value(item)
        if not isinstance(key, str) or not key or text is None:
            raise _fail(param, value)
        if not isinstance(item, str):
            note = note or "non-string values to strings"
        result[key] = text
    return result, note


def coerce(
    arguments: Mapping[str, Any], params: Sequence[Param]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Coerce known parameters; unknown parameters are dropped and recorded."""
    by_name = {p.name: p for p in params}
    values: dict[str, Any] = {}
    coercions: list[dict[str, Any]] = []
    for name, value in arguments.items():
        param = by_name.get(name)
        if param is None:
            coercions.append({"param": name, "coercion": "unknown parameter ignored"})
            continue
        if value is None:
            coercions.append({"param": name, "coercion": "null treated as omitted"})
            continue
        coerced, note = coerce_value(param, value)
        values[name] = coerced
        if note is not None:
            coercions.append({"param": name, "coercion": note, "received": _preview(value)})
    return values, coercions
