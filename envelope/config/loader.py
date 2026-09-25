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
    values_seen: dict[str, Any] = {}
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
        settings = apply_layer(settings, values)
        values_seen = values
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
    settings = apply_layer(settings, env_values)

    flag_values = {
        key: parse_value(key, value, text=True, source="command line --" + key.replace("_", "-"))
        for key, value in flags.items()
    }
    settings = apply_layer(settings, flag_values)

    explicit_default = "default_timeout_s" in {**values_seen, **env_values, **flag_values}
    if not explicit_default and settings.default_timeout_s > settings.max_timeout_s:
        # Lowering only the maximum also lowers the built-in default.
        settings = apply_layer(settings, {"default_timeout_s": settings.max_timeout_s})
    check_consistency(settings, "configuration")
    return LoadResult(settings, config_file, searched, warnings)
