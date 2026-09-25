"""OS-standard per-user locations for configuration, state and cache."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "envelope"


def _home() -> Path:
    return Path.home()


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
        return Path(base) / APP
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / APP


def default_config_file() -> Path:
    return config_dir() / "config.toml"


def state_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
        return Path(base) / APP / "state"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / APP / "state"
    base = os.environ.get("XDG_STATE_HOME") or str(_home() / ".local" / "state")
    return Path(base) / APP


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
        return Path(base) / APP / "cache"
    if sys.platform == "darwin":
        return _home() / "Library" / "Caches" / APP
    base = os.environ.get("XDG_CACHE_HOME") or str(_home() / ".cache")
    return Path(base) / APP


def default_audit_log() -> Path:
    return state_dir() / "audit.jsonl"


def default_spool_dir() -> Path:
    return cache_dir() / "spool"
