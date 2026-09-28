"""Assemble a trial's event timeline from the gateway ledger, audit log, trajectory and unit.

Ledger, audit and unit events carry wall-clock timestamps. Events derived from the trajectory
only have an order, so their timestamps are ordinals (`detail["ordinal"]`); attribution only
compares timestamps between events of the same kind pair, so the two scales never mix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .metrics import Event

SHELL_TOOLS = {"run", "run_shell_command", "shell", "bash", "mcp__envelope__run"}
TOOL_CALLS = {"tool_call", "tool_use"}
TOOL_RESULTS = {"tool_result", "tool_response"}
PROBLEM_KINDS = {
    "timeout",
    "gateway_error",
    "tool_error",
    "harness_error",
    "malformed_call",
    "runaway",
}


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


MALFORMED_MARKERS = ("invalid arguments for", "invalid tool call", "malformed tool call")


def _malformed_by_model(error: Any) -> bool:
    text = str(error.get("message", "") if isinstance(error, dict) else error or "").lower()
    return any(marker in text for marker in MALFORMED_MARKERS)


def ledger_events(rows: list[dict[str, Any]], in_flight: bool = False) -> list[Event]:
    """Request start/end and upstream failures; a later success recovers an earlier failure."""
    events: list[Event] = []
    ordered = sorted(rows, key=lambda row: row.get("timestamp", 0))
    for index, row in enumerate(ordered):
        end = float(row.get("timestamp", 0))
        start = end - float(row.get("duration") or 0)
        events.append(Event(start, "request_start", "model", {"role": row.get("role")}))
        events.append(Event(end, "request_end", "model", {"end_reason": row.get("end_reason")}))
        error = row.get("error")
        if error == "runaway":
            events.append(Event(end, "runaway", "model", {}))
        elif _malformed_by_model(error):
            # The provider refused the model's own tool-call JSON (a 502 for the harness). The
            # harness retries the same history, so it repeats: a model failure, not infrastructure.
            events.append(
                Event(end, "malformed_call", "model", {"reason": "provider rejected tool call"})
            )
        elif error == "disallowed_model" or row.get("model_mismatch"):
            events.append(Event(end, "gateway_error", "model", {"reason": error or "mismatch"}))
        elif error or row.get("end_reason") not in (None, "complete"):
            recovered = any(later.get("end_reason") == "complete" for later in ordered[index + 1 :])
            detail = {"recovered": recovered, "reason": row.get("end_reason")}
            events.append(Event(end, "gateway_error", "model", detail))
    if in_flight:
        events.append(Event(float("inf"), "request_start", "model", {"open": True}))
    return events


def audit_events(rows: list[dict[str, Any]]) -> tuple[list[Event], list[dict[str, Any]]]:
    """Tool start/end/error events plus the metric-shaped events (hints, coercions, errors).

    Envelope writes a `decision` record when a command is authorized and a `tool_call` record
    when it finishes, so a decision with no later tool_call was still running when the trial
    ended.
    """
    events: list[Event] = []
    metric: list[dict[str, Any]] = []
    for row in rows:
        stamp = _epoch(row["ts"]) if isinstance(row.get("ts"), str) else 0.0
        if row.get("event") == "decision":
            events.append(Event(stamp, "tool_start", "tool", {"source": "audit"}))
        elif row.get("event") == "tool_call":
            outcome = row.get("outcome") or {}
            if row.get("tool") == "run":  # only `run` has a decision record to pair with
                events.append(Event(stamp, "tool_end", "tool", {"tool": "run"}))
            for _ in row.get("coercions") or []:
                metric.append({"kind": "coercion"})
            for _ in outcome.get("hints") or []:
                metric.append({"kind": "hint"})
            if outcome.get("error"):
                events.append(Event(stamp, "tool_error", "tool", {"error": outcome["error"]}))
                metric.append({"kind": "tool_error"})
    return events, metric


def trajectory_tool_events(trajectory: list[dict[str, Any]]) -> list[Event]:
    """Shell tool start/end pairs by order, for arms without an envelope audit log."""
    events: list[Event] = []
    open_calls: dict[str, int] = {}
    for ordinal, item in enumerate(trajectory):
        kind = item.get("type")
        if kind in TOOL_CALLS and item.get("name") in SHELL_TOOLS:
            open_calls[str(item.get("id", ordinal))] = ordinal
            events.append(Event(float(ordinal), "tool_start", "tool", {"ordinal": True}))
        elif kind in TOOL_RESULTS:
            call = str(item.get("id", item.get("tool_call_id", "")))
            if open_calls.pop(call, None) is not None:
                events.append(Event(float(ordinal), "tool_end", "tool", {"ordinal": True}))
                if item.get("is_error"):
                    events.append(Event(float(ordinal), "tool_error", "tool", {"ordinal": True}))
    return events


def has_problem(events: list[Event]) -> bool:
    return any(event.kind in PROBLEM_KINDS for event in events)


def as_dicts(events: list[Event]) -> list[dict[str, Any]]:
    """JSON-safe events; "still open at the end of the trial" has a null timestamp."""
    return [
        {
            "timestamp": None if e.timestamp == float("inf") else e.timestamp,
            "kind": e.kind,
            "phase": e.phase,
            "detail": e.detail,
        }
        for e in events
    ]
