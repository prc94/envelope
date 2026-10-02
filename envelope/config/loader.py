"""Load settings from defaults < TOML file < ENVELOPE_* env vars < CLI flags."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from envelope import paths
from envelope.config.settings import (
    KNOWN_KEYS,
    ConfigError,
    Settings,
    apply_layer,
    check_consistency,
    parse_value,
)

ENV_PREFIX = "ENVELOPE_"
ENV_CONFIG = "ENVELOPE_CONFIG"


@dataclass
class LoadResult:
    settings: Settings
    config_file: Path | None
    searched: list[Path]
    warnings: list[str] = field(default_factory=lambda: list[str]())


def env_var_name(key: str) -> str:
    return ENV_PREFIX + key.upper()


def _resolve_max_concurrency_alias(
    values: dict[str, Any], source: str, warnings: list[str]
) -> dict[str, Any]:
    """`max_concurrency` is a deprecated alias of `max_running`, resolved per source."""
    if "max_concurrency" not in values:
        return values
    alias_value = values.pop("max_concurrency")
    if "max_running" in values:
        raise ConfigError(
            f"{source}: both max_concurrency and max_running are set; max_concurrency is a "
            "deprecated alias of max_running, so only one may be given"
        )
    warnings.append(f"{source}: max_concurrency is deprecated; use max_running instead")
    values["max_running"] = alias_value
    return values


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path}: invalid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"config file {path}: cannot read: {exc.strerror or exc}") from None


def _flatten_policy(data: dict[str, Any]) -> dict[str, Any]:
    """Accept policy settings either top-level or in a ``[policy]`` table."""
    policy = data.get("policy")
    if isinstance(policy, dict):
        merged = {k: v for k, v in data.items() if k != "policy"}
        for key, value in policy.items():  # pyright: ignore[reportUnknownVariableType]
            merged[str(key)] = value  # pyright: ignore[reportUnknownArgumentType]
        return merged
    return data


def load(
    *,
    flags: Mapping[str, str] | None = None,
    environ: Mapping[str, str],
    config_path: str | None = None,
) -> LoadResult:
    """Build effective settings. Raises ConfigError on invalid values."""
    flags = flags or {}
    warnings: list[str] = []
    settings = Settings()

    explicit = config_path or environ.get(ENV_CONFIG)
    candidate = Path(explicit).expanduser() if explicit else paths.default_config_file()
    searched = [candidate]
    config_file: Path | None = None
    if candidate.is_file():
        config_file = candidate
        raw = _flatten_policy(_read_toml(candidate))
        source = f"config file {candidate}"
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in KNOWN_KEYS:
                warnings.append(f"{source}: unknown key '{key}' ignored")
                continue
            values[key] = parse_value(key, value, text=False, source=source)
        values = _resolve_max_concurrency_alias(values, source, warnings)
        settings = apply_layer(settings, values)
    elif explicit:
        raise ConfigError(f"config file {candidate} does not exist")

    env_values: dict[str, Any] = {}
    known_env = {env_var_name(key): key for key in KNOWN_KEYS}
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX) or name == ENV_CONFIG:
            continue
        key = known_env.get(name)
        if key is None:
            warnings.append(f"environment: unknown variable '{name}' ignored")
            continue
        env_values[key] = parse_value(key, value, text=True, source=f"environment {name}")
    env_values = _resolve_max_concurrency_alias(env_values, "environment", warnings)
    settings = apply_layer(settings, env_values)

    flag_values = {
        key: parse_value(key, value, text=True, source="command line --" + key.replace("_", "-"))
        for key, value in flags.items()
    }
    flag_values = _resolve_max_concurrency_alias(flag_values, "command line", warnings)
    settings = apply_layer(settings, flag_values)

    check_consistency(settings, "configuration")
    if settings.wait_max_s >= 600:
        warnings.append(
            f"wait_max_s = {settings.wait_max_s} is at or above 600s; Qwen Code's absolute "
            "10-minute call limit leaves little margin"
        )
    return LoadResult(settings, config_file, searched, warnings)
