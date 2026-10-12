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
    gateway_errors = [e for e in events if e.kind == "gateway_error"]
    if gateway_errors and all(e.detail.get("recovered", False) for e in gateway_errors):
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


def process_metrics(audit_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Lifecycle counts from envelope's audit, including asynchronous endings."""
    calls = [row for row in audit_rows if row.get("event") == "tool_call"]
    handed_back = {
        outcome["handle"]
        for row in calls
        if row.get("tool") == "run"
        and (outcome := row.get("outcome") or {}).get("state") == "running"
        and row.get("executes") is not False
        and isinstance(outcome.get("handle"), str)
    }
    ended = {
        row["handle"]
        for row in audit_rows
        if row.get("event") == "process_end"
        and row.get("end_reason") != "shutdown"
        and isinstance(row.get("handle"), str)
    }
    stopped = {
        outcome["handle"]
        for row in calls
        if row.get("tool") == "stop"
        and isinstance((outcome := row.get("outcome") or {}).get("handle"), str)
    }
    waits = [
        {"condition": wait.get("condition", "exit"), "outcome": wait.get("outcome")}
        for row in calls
        if isinstance((wait := row.get("wait") or (row.get("outcome") or {}).get("wait")), dict)
    ]
    return {
        "handed_back_runs": len(handed_back),
        "joins": sum(row.get("tool") == "run" and row.get("executes") is False for row in calls),
        "waits": waits,
        "stop_calls": sum(row.get("tool") == "stop" for row in calls),
        "processes_calls": sum(row.get("tool") == "processes" for row in calls),
        "evictions": sum(
            row.get("event") == "process_end" and row.get("end_reason") == "eviction"
            for row in audit_rows
        ),
        "notices": sum(row.get("event") == "completion_notice" for row in audit_rows),
        "running_at_agent_end": len(handed_back - ended - stopped),
    }


def lever_metrics(
    events: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        **process_metrics(audit_rows or []),
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
