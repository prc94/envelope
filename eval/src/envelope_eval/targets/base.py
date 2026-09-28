from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class FingerprintMismatch(RuntimeError):
    """A target's measured fingerprint differs from what the job declared."""

    def __init__(self, target: str, mismatches: list[str]) -> None:
        super().__init__(
            f"target {target!r} does not match its declared fingerprint: " + "; ".join(mismatches)
        )
        self.target, self.mismatches = target, mismatches


@dataclass(frozen=True)
class Fingerprint:
    """What was measured on a target: OS build, code pages, versions, binary and policy hashes."""

    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.facts, sort_keys=True).encode()).hexdigest()

    def mismatches(self, declared: dict[str, Any]) -> list[str]:
        """Differences from a declaration; a declaration names only the facts a job cares about
        (nested tables are compared key by key)."""
        return _diff(declared, self.facts, "")


def _diff(declared: dict[str, Any], measured: dict[str, Any], prefix: str) -> list[str]:
    out: list[str] = []
    for key, want in declared.items():
        name = f"{prefix}{key}"
        have = measured.get(key)
        if isinstance(want, dict):
            out += _diff(want, have if isinstance(have, dict) else {}, name + ".")
        elif str(have) != str(want):  # a TOML integer may declare a registry string
            out.append(f"{name}: declared {want!r}, measured {have!r}")
    return out


def verify_fingerprint(name: str, target: Target, declared: dict[str, Any]) -> Fingerprint:
    """Measure `target` and refuse (raise) on any mismatch with the job's declaration."""
    measured = target.fingerprint()
    bad = measured.mismatches(declared)
    if bad:
        raise FingerprintMismatch(name, bad)
    return measured


class Target(Protocol):
    def run(self, argv: list[str], timeout: float | None = None) -> tuple[int, bytes, bytes]: ...
    def put(self, source: Path, destination: str) -> None: ...
    def get(self, source: str, destination: Path) -> None: ...
    def fingerprint(self) -> Fingerprint: ...
