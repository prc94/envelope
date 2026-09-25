"""Settings model: every configurable value, its default and its validation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, cast

from envelope.shells.ids import BUILTIN_SHELLS, canonical_shell_id

DEFAULT_ENV: Mapping[str, str] = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "DEBIAN_FRONTEND": "noninteractive",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
}

DEFAULT_SCRUB: tuple[str, ...] = ("*_API_KEY", "*_TOKEN", "*_SECRET", "OPENROUTER_*")

LOG_LEVELS: tuple[str, ...] = ("error", "warn", "info", "debug")

MIB = 1024 * 1024


class ConfigError(Exception):
    """Invalid configuration; envelope must not start."""


@dataclass(frozen=True)
class CustomShell:
    id: str
    path: str
    args: tuple[str, ...]
    extension: str | None = None


@dataclass(frozen=True)
class Settings:
    default_shell: str | None = None
    shells: Mapping[str, CustomShell] = field(default_factory=lambda: dict[str, CustomShell]())
    default_cwd: str | None = None
    default_timeout_s: float = 120.0
    max_timeout_s: float = 3600.0
    max_concurrency: int = 8
    progress_interval_s: float = 5.0
    env_defaults: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ENV))
    env_scrub: tuple[str, ...] = DEFAULT_SCRUB
    output_max_chars: int = 12_000
    output_max_lines: int = 400
    spool_dir: str | None = None
    spool_cap_mib: float = 64.0
    spool_total_mib: float = 1024.0
    spool_retention_h: float = 24.0
    allowed_shells: tuple[str, ...] | None = None
    allowed_cwd_roots: tuple[str, ...] | None = None
    max_command_chars: int = 1_000_000
    audit_only: bool = False
    audit_enabled: bool = True
    audit_log: str | None = None
    log_level: str = "warn"

    @property
    def spool_cap_bytes(self) -> int:
        return int(self.spool_cap_mib * MIB)

    @property
    def spool_total_bytes(self) -> int:
        return int(self.spool_total_mib * MIB)


KNOWN_KEYS: tuple[str, ...] = tuple(f.name for f in fields(Settings))


# --- value parsing -----------------------------------------------------------

Parser = Callable[[str, Any, bool], Any]
"""(key, raw value, raw_is_text) -> parsed value; raises ValueError(expected form)."""


def _number(positive: bool, integer: bool) -> Parser:
    kind = "integer" if integer else "number"
    expected = f"must be a positive {kind}" if positive else f"must be a non-negative {kind}"

    def parse(key: str, value: Any, text: bool) -> Any:
        if text and isinstance(value, str):
            try:
                value = int(value) if integer else float(value)
            except ValueError:
                raise ValueError(expected) from None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(expected)
        if integer and not isinstance(value, int):
            if float(value).is_integer():
                value = int(value)
            else:
                raise ValueError(expected)
        if value < 0 or (positive and value == 0):
            raise ValueError(expected)
        return value if integer else float(value)

    return parse


def _bool(key: str, value: Any, text: bool) -> bool:
    if isinstance(value, bool):
        return value
    if text and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ValueError("must be true or false")


def _opt_str(key: str, value: Any, text: bool) -> str | None:
    if not isinstance(value, str):
        raise ValueError("must be a string")
    return value or None


def _level(key: str, value: Any, text: bool) -> str:
    if isinstance(value, str) and value.strip().lower() in LOG_LEVELS:
        return value.strip().lower()
    raise ValueError("must be one of " + ", ".join(LOG_LEVELS))


def _json_or(value: str) -> Any:
    stripped = value.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return None


def _str_list(key: str, value: Any, text: bool) -> tuple[str, ...]:
    expected = "must be a list of strings"
    if text and isinstance(value, str):
        decoded = _json_or(value)
        if decoded is None:
            value = [part.strip() for part in value.split(",") if part.strip()]
        else:
            value = decoded
    if not isinstance(value, list):
        raise ValueError(expected)
    items = cast(list[Any], value)
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(expected)
    return tuple(cast(list[str], items))


def _opt_str_list(key: str, value: Any, text: bool) -> tuple[str, ...] | None:
    return _str_list(key, value, text)


def _patterns(key: str, value: Any, text: bool) -> tuple[str, ...]:
    patterns = _str_list(key, value, text)
    for pattern in patterns:
        if not re.fullmatch(r"[A-Za-z0-9_*?]+", pattern):
            raise ValueError(
                "must be a list of environment variable name patterns "
                "(letters, digits, '_', '*' and '?' only)"
            )
    return patterns


def _str_map(key: str, value: Any, text: bool) -> dict[str, str]:
    expected = "must be a table of string values (or KEY=value lines)"
    if text and isinstance(value, str):
        decoded = _json_or(value)
        if decoded is None:
            pairs: dict[str, str] = {}
            for line in re.split(r"[\n;]", value):
                if not line.strip():
                    continue
                name, sep, val = line.partition("=")
                if not sep or not name.strip():
                    raise ValueError(expected)
                pairs[name.strip()] = val
            return pairs
        value = decoded
    if not isinstance(value, dict):
        raise ValueError(expected)
    mapping = cast(dict[Any, Any], value)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ValueError(expected)
    return cast(dict[str, str], mapping)


def _shell_id(key: str, value: Any, text: bool) -> str | None:
    if not isinstance(value, str):
        raise ValueError("must be a shell identifier")
    return canonical_shell_id(value) if value.strip() else None


def _shell_ids(key: str, value: Any, text: bool) -> tuple[str, ...] | None:
    return tuple(canonical_shell_id(item) for item in _str_list(key, value, text))


def _shells(key: str, value: Any, text: bool) -> dict[str, CustomShell]:
    expected = (
        "must be a table of shells, each with 'path' (string), 'args' (list of strings "
        "containing '{script}' or '{file}') and optional 'extension' (e.g. '.nu')"
    )
    if text and isinstance(value, str):
        value = _json_or(value)
    if not isinstance(value, dict):
        raise ValueError(expected)
    result: dict[str, CustomShell] = {}
    for raw_id, raw_def in cast(dict[Any, Any], value).items():
        if not isinstance(raw_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", raw_id):
            raise ValueError(expected)
        shell_id = raw_id.lower()
        if shell_id in BUILTIN_SHELLS:
            raise ValueError(f"cannot redefine built-in shell '{shell_id}'")
        if not isinstance(raw_def, dict):
            raise ValueError(expected)
        definition = cast(dict[str, Any], raw_def)
        path = definition.get("path")
        args = definition.get("args")
        extension = definition.get("extension")
        unknown = set(definition) - {"path", "args", "extension"}
        if unknown or not isinstance(path, str) or not path:
            raise ValueError(expected)
        if not isinstance(args, list) or not all(isinstance(a, str) for a in cast(list[Any], args)):
            raise ValueError(expected)
        arg_list = cast(list[str], args)
        if not any("{script}" in a or "{file}" in a for a in arg_list):
            raise ValueError(expected)
        if extension is not None and (
            not isinstance(extension, str) or not re.fullmatch(r"\.[A-Za-z0-9_]+", extension)
        ):
            raise ValueError(expected)
        result[shell_id] = CustomShell(shell_id, path, tuple(arg_list), extension)
    return result


PARSERS: dict[str, Parser] = {
    "default_shell": _shell_id,
    "shells": _shells,
    "default_cwd": _opt_str,
    "default_timeout_s": _number(True, False),
    "max_timeout_s": _number(True, False),
    "max_concurrency": _number(True, True),
    "progress_interval_s": _number(True, False),
    "env_defaults": _str_map,
    "env_scrub": _patterns,
    "output_max_chars": _number(True, True),
    "output_max_lines": _number(True, True),
    "spool_dir": _opt_str,
    "spool_cap_mib": _number(True, False),
    "spool_total_mib": _number(True, False),
    "spool_retention_h": _number(False, False),
    "allowed_shells": _shell_ids,
    "allowed_cwd_roots": _opt_str_list,
    "max_command_chars": _number(True, True),
    "audit_only": _bool,
    "audit_enabled": _bool,
    "audit_log": _opt_str,
    "log_level": _level,
}

assert set(PARSERS) == set(KNOWN_KEYS)


def describe(value: Any) -> str:
    return json.dumps(value) if isinstance(value, str) else repr(value)


def parse_value(key: str, value: Any, *, text: bool, source: str) -> Any:
    try:
        return PARSERS[key](key, value, text)
    except ValueError as exc:
        raise ConfigError(f"{source}: {key} = {describe(value)}: {key} {exc}") from None


def apply_layer(base: Settings, values: Mapping[str, Any]) -> Settings:
    return replace(base, **values)


def check_consistency(settings: Settings, source: str) -> None:
    """Cross-field checks and shell identifiers (after all layers are merged)."""
    if settings.default_timeout_s > settings.max_timeout_s:
        raise ConfigError(
            f"{source}: default_timeout_s = {settings.default_timeout_s}: default_timeout_s "
            f"must not exceed max_timeout_s ({settings.max_timeout_s})"
        )
    known = set(BUILTIN_SHELLS) | set(settings.shells)
    if settings.default_shell is not None and settings.default_shell not in known:
        raise ConfigError(
            f"{source}: default_shell = {describe(settings.default_shell)}: default_shell must "
            f"be one of {', '.join(sorted(known))}"
        )
    for shell_id in settings.allowed_shells or ():
        if shell_id not in known:
            raise ConfigError(
                f"{source}: allowed_shells contains {describe(shell_id)}: allowed_shells must "
                f"contain only {', '.join(sorted(known))}"
            )
