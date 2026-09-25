"""Child environment: inherit, scrub secrets, apply non-interactive defaults, then
explicit caller values (which win and are never scrubbed)."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


@dataclass
class BuiltEnv:
    env: dict[str, str]
    removed: list[str] = field(default_factory=lambda: list[str]())


def build_env(
    inherited: Mapping[str, str],
    *,
    scrub: Sequence[str],
    defaults: Mapping[str, str],
    explicit: Mapping[str, str] | None,
    windows: bool,
) -> BuiltEnv:
    explicit = explicit or {}
    env: dict[str, str] = {}
    removed: list[str] = []
    for name, value in inherited.items():
        # Windows env names are case-insensitive; match patterns accordingly.
        probe = name.upper() if windows else name
        if any(fnmatch.fnmatchcase(probe, p.upper() if windows else p) for p in scrub):
            removed.append(name)
            continue
        env[name] = value
    for name, value in defaults.items():
        _set(env, name, value, windows)
    for name, value in explicit.items():
        _set(env, name, value, windows)
    return BuiltEnv(env, sorted(removed))


def _set(env: dict[str, str], name: str, value: str, windows: bool) -> None:
    if windows:
        for existing in [k for k in env if k.upper() == name.upper() and k != name]:
            del env[existing]
    env[name] = value
