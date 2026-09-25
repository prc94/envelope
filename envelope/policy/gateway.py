"""The single execution gateway: every request is decided and audited before it
can run. Execution code only accepts a ``Permit``, which only the gateway mints."""

from __future__ import annotations

from dataclasses import dataclass

from envelope.audit import AuditLog
from envelope.config.settings import Settings
from envelope.policy.decide import DecideFn
from envelope.policy.model import Decision, Request

_MINT = object()


class PolicyRefusal(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.rule)
        self.decision = decision


@dataclass(frozen=True, init=False)
class Permit:
    request: Request
    decision: Decision

    def __init__(self, request: Request, decision: Decision, *, _token: object = None) -> None:
        if _token is not _MINT:
            raise RuntimeError("a Permit can only be issued by the execution gateway")
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "decision", decision)


class Gateway:
    def __init__(self, settings: Settings, audit: AuditLog, decide: DecideFn) -> None:
        self.settings = settings
        self.audit = audit
        self._decide = decide

    def authorize(self, request: Request) -> Permit:
        """Decide, record the decision, and return a Permit or raise PolicyRefusal."""
        decision = self._decide(request, self.settings)
        if self.settings.audit_only and decision.verdict != "allow":
            decision = Decision(
                decision.verdict, decision.rule, decision.reasons, decision.remedy, enforced=False
            )
        self.audit.write(
            {"event": "decision", "request": request.to_json(), "decision": decision.to_json()}
        )
        if decision.blocks:
            raise PolicyRefusal(decision)
        return Permit(request, decision, _token=_MINT)


def refusal_text(decision: Decision) -> str:
    reasons = "; ".join(decision.reasons)
    if decision.verdict == "deny":
        lines = [f"denied by policy (rule {decision.rule}): {reasons}"]
        if decision.remedy:
            lines.append(f"to allow it: {decision.remedy}")
    else:
        lines = [
            f"approval required by policy (rule {decision.rule}): {reasons}",
            "The command was not run: this MCP client cannot ask the user for approval.",
        ]
        if decision.remedy:
            lines.append(f"to allow it without asking: {decision.remedy}")
    lines.append(
        "The command was not executed. Tell the user if they need to change the configuration."
    )
    return "\n".join(lines)


def audit_only_note(decision: Decision) -> str | None:
    if decision.enforced or decision.verdict == "allow":
        return None
    would = "denied" if decision.verdict == "deny" else "required approval"
    return (
        f"note: policy audit-only mode: this request would have been {would} "
        f"(rule {decision.rule}: {'; '.join(decision.reasons)})"
    )
