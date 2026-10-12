"""The ``stop`` tool: terminate a background process gracefully, then forcibly."""

from __future__ import annotations

import asyncio
from typing import Any

from envelope.audit import call_record
from envelope.config.settings import Settings
from envelope.output.spool import HandleError
from envelope.output.view import DEFAULT_TAIL, tail_view, unread_view
from envelope.protocol.server import CallContext, ToolResult
from envelope.render.result import RunReport, header
from envelope.services import Services
from envelope.tools.coercion import ArgumentError, Param, coerce

PARAMS = (
    Param("handle", "string", "must be an output handle or a name (a string)"),
    Param("grace_s", "number", "must be 0 or a positive number of seconds"),
)


def stop_definition(settings: Settings) -> dict[str, Any]:
    return {
        "name": "stop",
        "title": "Stop a background process",
        "description": (
            "Stop a background process by run handle or name. Request a graceful stop, then "
            "force termination after grace_s. Also accepts an already finished process."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Run handle (out_...) or process name.",
                },
                "grace_s": {
                    "type": "number",
                    "description": (
                        "Grace period before force ("
                        f"default {settings.stop_grace_s:g}s, max {settings.stop_grace_max_s:g}s)."
                    ),
                },
            },
            "required": ["handle"],
        },
        "annotations": {
            "title": "Stop a background process",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


class StopTool:
    name = "stop"

    def __init__(self, services: Services) -> None:
        self.services = services
        self._definition = stop_definition(services.settings)

    def definition(self) -> dict[str, Any]:
        return self._definition

    async def call(self, arguments: dict[str, Any], ctx: CallContext) -> ToolResult:
        coercions: list[dict[str, Any]] = list(ctx.extra.get("coercions", []))
        try:
            values, more = coerce(arguments, PARAMS)
            coercions.extend(more)
            handle = values.get("handle")
            if not isinstance(handle, str) or not handle.strip():
                raise ValueError(
                    "`handle` is required (the out_... handle or name from a run result)"
                )
            resolved = self.services.processes.resolve_handle(handle.strip()) or handle.strip()
        except (ArgumentError, ValueError) as exc:
            self._audit(ctx, arguments, coercions, {"error": str(exc)})
            return self.services.attach_notice(
                ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
            )

        settings = self.services.settings
        grace_value = values.get("grace_s")
        grace_s = float(grace_value) if grace_value is not None else settings.stop_grace_s
        grace_s = max(0.0, min(grace_s, settings.stop_grace_max_s))

        managed = self.services.processes.get(resolved)
        graceful: bool | None = None
        if managed is not None and not managed.finished:
            # Cancelling `stop` still completes it (mcp-server Cancellation).
            graceful = await asyncio.shield(managed.stop(grace_s))
        if managed is not None:
            self.services.finalize_process(managed)

        try:
            reader = self.services.spool.open(resolved)
        except HandleError as exc:
            self._audit(ctx, values, coercions, {"error": str(exc)})
            return self.services.attach_notice(
                ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
            )

        budget = (settings.output_max_chars, settings.output_max_lines)
        position = self.services.spool.read_position(resolved)
        if position >= reader.total_lines:
            view = tail_view(reader, DEFAULT_TAIL, "both", *budget)
        else:
            view = unread_view(reader, position, "both", *budget)
            last_line = view.structured.get("last_line")
            if last_line is not None:
                self.services.spool.advance_read_position(resolved, last_line)

        exit_code = signal = None
        name = None
        if managed is not None and managed.outcome is not None:
            exit_code, signal, name = (
                managed.outcome.exit_code,
                managed.outcome.signal,
                managed.name,
            )
        else:
            # Already finished and reported before this call even started
            # (design: `Services.finalize_process` persists the final status
            # into the spool's own meta.json).
            info = reader.meta.info
            if "exit_code" in info:
                exit_code, signal, name = info["exit_code"], info["signal"], info.get("name")

        notes: list[str] = []
        if graceful is not None:
            notes.append(f"note: stop was {'graceful' if graceful else 'forced'}")
        else:
            notes.append("note: process had already finished")

        header_report = RunReport(
            shell=reader.meta.info.get("shell", ""),
            cwd=reader.meta.info.get("cwd", ""),
            duration_s=0.0,
            exit_code=exit_code,
            signal=signal,
        )
        text = "\n".join([header(header_report), *notes, view.text])
        structured: dict[str, Any] = {
            "exit_code": exit_code,
            "signal": signal,
            **({"name": name} if name is not None else {}),
            **({"graceful": graceful} if graceful is not None else {}),
            **view.structured,
        }
        self._audit(
            ctx,
            values,
            coercions,
            {"handle": resolved, "graceful": graceful, "exit_code": exit_code, "signal": signal},
        )
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
