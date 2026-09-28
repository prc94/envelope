"""Symmetric event attribution rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Attribution = Literal["model", "tool", "harness", "infra", "indeterminate"]


@dataclass(frozen=True)
class Evidence:
    timed_out: bool = False
    tool_in_flight: bool = False
    tool_error: bool = False
    upstream_error: bool = False
    harness_error: bool = False
    request_in_flight: bool = False
    malformed_call: bool = False
    runaway: bool = False


def attribute(e: Evidence) -> Attribution:
    causes = []
    if e.upstream_error:
        causes.append("infra")
    if e.timed_out and (e.tool_in_flight or e.tool_error):
        causes.append("tool")
    if e.harness_error and not e.tool_in_flight:
        causes.append("harness")
    if e.runaway or e.malformed_call:
        causes.append("model")
    if len(set(causes)) > 1:
        return "indeterminate"
    if causes:
        return causes[0]  # type: ignore[return-value]
    if e.timed_out and (e.request_in_flight or not e.tool_in_flight):
        return "harness"
    return "indeterminate"
