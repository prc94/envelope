"""Timeline attribution and transcript-derived invariants/lever metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .attribution import Attribution, Evidence, attribute


@dataclass(frozen=True)
class Event:
    timestamp: float
    kind: str
    phase: str
    detail: dict[str, Any]


def timeline_attribution(events: list[Event]) -> Attribution:
    if any(e.kind == "gateway_error" and e.detail.get("recovered", False) for e in events):
        return "model"
    evidence = Evidence(
        timed_out=any(e.kind == "timeout" for e in events),
        tool_in_flight=any(
            e.kind == "tool_start"
            and not any(x.kind == "tool_end" and x.timestamp >= e.timestamp for x in events)
            for e in events
        ),
        tool_error=any(e.kind == "tool_error" for e in events),
        upstream_error=any(
            e.kind == "gateway_error" and not e.detail.get("recovered", False) for e in events
        ),
        harness_error=any(e.kind == "harness_error" for e in events),
        request_in_flight=any(
            e.kind == "request_start"
            and not any(x.kind == "request_end" and x.timestamp >= e.timestamp for x in events)
            for e in events
        ),
        malformed_call=any(e.kind == "malformed_call" for e in events),
        runaway=any(e.kind == "runaway" for e in events),
    )
    return attribute(evidence)


def lever_metrics(events: list[dict[str, Any]], trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(event.get("text", event.get("content", ""))) for event in trajectory]
    joined = "\n".join(texts)
    tool_calls = [event for event in trajectory if event.get("type") in {"tool_call", "tool_use"}]
    shell_calls = [
        event
        for event in tool_calls
        if event.get("name") in {"run", "run_shell_command", "shell", "bash"}
    ]
    output_calls = [event for event in tool_calls if event.get("name") == "output"]
    hints = [event for event in events if event.get("kind") == "hint"]
    commands = [str(event.get("arguments", {}).get("command", "")) for event in shell_calls]
    return {
        "truncated": "output truncated" in joined.lower() or "truncated" in joined.lower(),
        "recovery_via_output": bool(output_calls),
        "hints_shown": len(hints),
        "replacement_characters": joined.count("\ufffd"),
        "tool_calls": len(tool_calls),
        "shell_calls": len(shell_calls),
        "tool_discovery_before_shell": sum(
            1 for event in tool_calls if event.get("name") in {"tool_search", "search_tools"}
        ),
        "main_model_turns": sum(
            1
            for event in events
            if event.get("kind") == "model_request" and event.get("auxiliary") is not True
        ),
        "main_model_tokens": sum(
            int(event.get("tokens", 0) or 0)
            for event in events
            if event.get("kind") == "model_request" and event.get("auxiliary") is not True
        ),
        "next_command_after_hint": commands[-1] if hints and commands else None,
        "coercions": sum(1 for event in events if event.get("kind") == "coercion"),
        "tool_errors": sum(1 for event in events if event.get("kind") == "tool_error"),
        "leftovers": sum(1 for event in events if event.get("kind") == "leftover"),
        "created_files": sum(1 for event in events if event.get("kind") == "created_file"),
        "invented_handles": sum(1 for event in events if event.get("kind") == "invented_handle"),
        "truncation_recovery": bool(output_calls) and ("truncated" in joined.lower()),
    }
