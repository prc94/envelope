"""Normalized requests and policy decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Verdict = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class ExecRequest:
    """Start a command in a shell."""

    shell: str
    shell_path: str
    command: str
    cwd: str
    env: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    """Only the variables explicitly supplied by the caller."""
    stdin_supplied: bool = False
    timeout_s: float = 0.0
    client: dict[str, Any] | None = None
    kind: Literal["exec"] = "exec"

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["command_chars"] = len(self.command)
        return data


@dataclass(frozen=True)
class InputRequest:
    """Send text to the standard input of a running process (used from P3 on)."""

    handle: str
    shell: str
    text: str
    client: dict[str, Any] | None = None
    kind: Literal["input"] = "input"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


Request = ExecRequest | InputRequest


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str
    reasons: tuple[str, ...]
    remedy: str | None = None
    """What the user can change to allow the request."""
    enforced: bool = True
    """False in audit-only mode: the verdict is logged but does not block."""

    @property
    def blocks(self) -> bool:
        return self.enforced and self.verdict != "allow"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "verdict": self.verdict,
            "rule": self.rule,
            "reasons": list(self.reasons),
        }
        if self.remedy:
            data["remedy"] = self.remedy
        if not self.enforced:
            data["audit_only"] = True
        return data
