"""The ``processes`` tool: list envelope-started processes (design B6/B8)."""

from __future__ import annotations

import time
from typing import Any

from envelope.audit import call_record
from envelope.execution.ports import listening_ports
from envelope.execution.runner import ManagedProcess
from envelope.protocol.server import CallContext, ToolResult
from envelope.render.result import format_duration
from envelope.services import Services
from envelope.tools.coercion import ArgumentError, Param, coerce

PARAMS = (Param("all", "boolean", "must be a boolean"),)


def processes_definition(limit: int) -> dict[str, Any]:
    return {
        "name": "processes",
        "title": "List background processes",
        "description": (
            "List background processes started by run() in this session: by default those still "
            "running and those that finished without their result being read yet; with all: true, "
            f"every one whose output handle is still valid. Shows at most {limit} entries, running "
            "first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "description": "Include processes already reported as finished (default: false).",
                },
            },
        },
        "annotations": {
            "title": "List background processes",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def _shorten(text: str, limit: int = 80) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _entry(managed: ManagedProcess, now: float) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "handle": managed.handle,
        "command": _shorten(managed.request.command),
    }
    if managed.name is not None:
        entry["name"] = managed.name
    if managed.finished:
        assert managed.outcome is not None
        entry["state"] = "ended"
        entry["exit_code"] = managed.outcome.exit_code
        entry["signal"] = managed.outcome.signal
        entry["duration_ms"] = round(managed.outcome.duration_s * 1000)
    else:
        entry["state"] = "running"
        entry["uptime_s"] = round(now - managed.started, 1)
        pids = managed.process.tree_pids()
        if pids is not None:
            ports = listening_ports(pids)
            if ports:
                entry["ports"] = sorted(ports)
    if managed.writer.last_line:
        entry["last_line"] = _shorten(managed.writer.last_line)
    return entry


class ProcessesTool:
    name = "processes"

    def __init__(self, services: Services) -> None:
        self.services = services
        self._definition = processes_definition(services.settings.processes_limit)

    def definition(self) -> dict[str, Any]:
        return self._definition

    async def call(self, arguments: dict[str, Any], ctx: CallContext) -> ToolResult:
        coercions: list[dict[str, Any]] = list(ctx.extra.get("coercions", []))
        try:
            values, more = coerce(arguments, PARAMS)
            coercions.extend(more)
        except ArgumentError as exc:
            self._audit(ctx, arguments, coercions, {"error": str(exc)})
            return self.services.attach_notice(
                ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
            )
        show_all = bool(values.get("all", False))
        limit = self.services.settings.processes_limit
        now = time.monotonic()

        all_processes = self.services.processes.all()
        candidates = all_processes if show_all else [m for m in all_processes if not m.final_seen]
        running = [m for m in candidates if not m.finished]
        finished = sorted(
            (m for m in candidates if m.finished),
            key=lambda m: m.outcome.duration_s + m.started if m.outcome else m.started,
            reverse=True,
        )
        ordered = running + finished
        shown = ordered[:limit]
        left_out = len(ordered) - len(shown)
        for managed in shown:
            if managed.finished:
                managed.final_seen = True

        if not shown:
            text = "no processes are running"
        else:
            lines = [f"{len(shown)} process{'es' if len(shown) != 1 else ''}:"]
            for entry in (_entry(m, now) for m in shown):
                state = (
                    f"exit {entry['exit_code']}"
                    if entry["state"] == "ended" and entry.get("signal") is None
                    else (
                        f"signal {entry['signal']}"
                        if entry["state"] == "ended"
                        else f"running {format_duration(entry['uptime_s'])}"
                    )
                )
                bits = [entry["handle"]]
                if "name" in entry:
                    bits.append(f'"{entry["name"]}"')
                bits.append(state)
                if "ports" in entry:
                    bits.append("port " + ",".join(str(p) for p in entry["ports"]))
                bits.append(entry["command"])
                if "last_line" in entry:
                    bits.append(f"last: {entry['last_line']}")
                lines.append(" | ".join(bits))
            if left_out:
                lines.append(f"note: {left_out} more not shown")
            text = "\n".join(lines)

        structured = {
            "count": len(shown),
            "left_out": left_out,
            "processes": [_entry(m, now) for m in shown],
        }
        self._audit(ctx, values, coercions, {"count": len(shown), "left_out": left_out})
        return self.services.attach_notice(ToolResult(text, structured))

    def _audit(
        self,
        ctx: CallContext,
        arguments: dict[str, Any],
        coercions: list[dict[str, Any]],
        outcome: dict[str, Any],
    ) -> None:
        self.services.audit.write(
            call_record(
                client=ctx.client_info,
                tool=self.name,
                arguments=arguments,
                coercions=coercions,
                request=None,
                decision=None,
                outcome=outcome,
                executes=False,
            )
        )
