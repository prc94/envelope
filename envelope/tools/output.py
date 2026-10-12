"""The ``output`` tool: page, tail or grep the spooled output of a previous run."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from envelope.audit import call_record
from envelope.execution.quiet import quiet_hint
from envelope.execution.wait import WaitCondition
from envelope.output.spool import HandleError, SpoolView
from envelope.output.view import (
    DEFAULT_LIMIT,
    DEFAULT_TAIL,
    StreamFilter,
    View,
    ViewError,
    grep_view,
    range_view,
    tail_view,
    unread_view,
)
from envelope.protocol.server import CallContext, ToolResult
from envelope.services import Services
from envelope.tools.coercion import ArgumentError, Param, coerce

PARAMS = (
    Param("handle", "string", "must be an output handle like out_1a2b3c4d, or a name (a string)"),
    Param("tail", "integer", "must be a whole number of lines"),
    Param("offset", "integer", "must be a 1-based line number"),
    Param("limit", "integer", "must be a whole number of lines"),
    Param("grep", "string", "must be a regular expression (a string)"),
    Param("context", "integer", "must be a whole number of context lines"),
    Param("stream", "choice", "must be one of both, stdout, stderr", ("both", "stdout", "stderr")),
    Param("wait_s", "number", "must be 0 or a positive number of seconds"),
    Param(
        "wait_for",
        "wait_for",
        "must be 'exit', 'regex:<pattern>', 'port:<n>', 'idle:<seconds>' or an object form",
    ),
)
_EXIT_CONDITION = WaitCondition("exit", "exit")
_WAIT_OUTCOME_NOTES = {
    "condition_met": "note: wait condition met",
    "finished": "note: the process finished before the wait condition was met",
    "wait_expired": "note: the wait expired before the condition was met",
}


def output_definition() -> dict[str, Any]:
    return {
        "name": "output",
        "title": "Read command output",
        "description": (
            "Read saved run output by handle or name, including lines omitted from a shortened "
            f"result. No mode returns unread lines (or the last {DEFAULT_TAIL} if none). Choose "
            f"one mode: tail, offset+limit (1-based; default limit {DEFAULT_LIMIT}), or regex "
            "grep+context. stream selects stdout, stderr or both. wait_s/wait_for can wait before "
            "reading. Lines include line numbers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Run handle (out_...) or process name.",
                },
                "tail": {"type": "integer", "description": "Last N lines."},
                "offset": {"type": "integer", "description": "First line (1-based)."},
                "limit": {
                    "type": "integer",
                    "description": "Lines from offset.",
                },
                "grep": {
                    "type": "string",
                    "description": "Regex for matching lines.",
                },
                "context": {
                    "type": "integer",
                    "description": "Context lines around grep matches.",
                },
                "stream": {
                    "type": "string",
                    "enum": ["both", "stdout", "stderr"],
                    "description": "Stream (default both).",
                },
                "wait_s": {
                    "type": "number",
                    "description": "Seconds to wait before reading; default condition is exit.",
                },
                "wait_for": {
                    "oneOf": [{"type": "string"}, {"type": "object"}],
                    "description": (
                        "Wait condition: exit (default), regex:<pattern>, port:<n>, or "
                        "idle:<seconds>."
                    ),
                },
            },
            "required": ["handle"],
        },
        "annotations": {
            "title": "Read command output",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


class OutputTool:
    name = "output"

    def __init__(self, services: Services) -> None:
        self.services = services
        self._definition = output_definition()

    def definition(self) -> dict[str, Any]:
        return self._definition

    async def call(self, arguments: dict[str, Any], ctx: CallContext) -> ToolResult:
        started = time.monotonic()
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
            wait_outcome = await self._maybe_wait(resolved, values)
            view = await asyncio.to_thread(self._view, {**values, "handle": resolved})
        except (ArgumentError, HandleError, ViewError, ValueError) as exc:
            self._audit(ctx, arguments, coercions, {"error": str(exc)})
            return self.services.attach_notice(
                ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
            )
        structured = dict(view.structured)
        self._add_process_state(structured, resolved)
        text = view.text
        managed = self.services.processes.get(resolved)
        if managed is not None and (
            hint := quiet_hint(managed, self.services.settings.input_hint_after_s)
        ):
            text = f"{text}\n{hint.text}"
            structured["hints"] = [*structured.get("hints", []), hint.id]
        if wait_outcome is not None:
            structured["wait_outcome"] = wait_outcome
            text = f"{text}\n{_WAIT_OUTCOME_NOTES[wait_outcome]}"
        self._audit(
            ctx,
            {**values, **({"wait_for": values["wait_for"].text} if "wait_for" in values else {})},
            coercions,
            {
                **{k: structured.get(k) for k in ("handle", "mode", "truncated")},
                **(
                    {
                        "wait": {
                            "condition": values["wait_for"].text
                            if "wait_for" in values
                            else "exit",
                            "outcome": wait_outcome,
                            "waited_ms": round((time.monotonic() - started) * 1000),
                        }
                    }
                    if wait_outcome is not None
                    else {}
                ),
            },
        )
        return self.services.attach_notice(ToolResult(text, structured))

    async def _maybe_wait(self, resolved: str, values: dict[str, Any]) -> str | None:
        wait_for_value = values.get("wait_for")
        wait_s_value = values.get("wait_s")
        if wait_for_value is None and wait_s_value is None:
            return None
        managed = self.services.processes.get(resolved)
        if managed is None or managed.finished:
            return None
        settings = self.services.settings
        condition: WaitCondition = wait_for_value if wait_for_value is not None else _EXIT_CONDITION
        wait_s = float(wait_s_value) if wait_s_value is not None else settings.wait_default_s
        wait_s = max(0.0, min(wait_s, settings.wait_max_s))
        position = self.services.spool.read_position(resolved)
        return await managed.wait_condition(
            condition, time.monotonic() + wait_s, from_line=position
        )

    def _add_process_state(self, structured: dict[str, Any], resolved: str) -> None:
        managed = self.services.processes.get(resolved)
        if managed is not None:
            if managed.finished:
                assert managed.outcome is not None
                structured["state"] = "ended"
                structured["exit_code"] = managed.outcome.exit_code
                structured["signal"] = managed.outcome.signal
            else:
                structured["state"] = "running"
            if managed.name is not None:
                structured["name"] = managed.name
            return
        # Not (or no longer) in the live table: the process already finished
        # and was reported, but its final status survives in the spool's own
        # meta.json (`Services.finalize_process`), bounded by normal spool
        # retention rather than an unbounded in-memory cache.
        try:
            info = self.services.spool.open(resolved).meta.info
        except HandleError:
            return
        if "exit_code" in info:
            structured["state"] = "ended"
            structured["exit_code"] = info["exit_code"]
            structured["signal"] = info["signal"]
            if "name" in info:
                structured["name"] = info["name"]

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

    def _view(self, values: dict[str, Any]) -> View:
        handle = values.get("handle")
        if not isinstance(handle, str) or not handle.strip():
            raise ValueError("`handle` is required (the out_... handle from a run result)")
        reader = self.services.spool.open(handle.strip())
        settings = self.services.settings
        stream = values.get("stream", "both")
        modes = [m for m in ("tail", "offset", "grep") if m in values]
        if (
            "limit" in values
            and "offset" not in values
            and "grep" not in values
            and "tail" not in values
        ):
            values["offset"] = 1
            modes.append("offset")
        if len(modes) > 1:
            raise ValueError(
                f"use exactly one read mode, not {' and '.join(modes)} (tail, offset/limit or grep)"
            )
        budget = (settings.output_max_chars, settings.output_max_lines)
        if not modes:
            # No explicit mode: "unread else tail" (design B4). Explicit modes
            # below never move the read position -- only this default does.
            return self._unread_or_tail(reader, handle.strip(), stream, budget)
        mode = modes[0]
        if mode == "grep":
            pattern = values["grep"]
            if not pattern:
                raise ValueError("`grep` must be a non-empty regular expression")
            return grep_view(reader, pattern, values.get("context", 0), stream, *budget)
        if mode == "offset":
            offset = values["offset"]
            limit = values.get("limit", DEFAULT_LIMIT)
            if offset < 1:
                raise ValueError("`offset` must be 1 or greater (line numbers are 1-based)")
            if limit < 1:
                raise ValueError("`limit` must be 1 or greater")
            return range_view(reader, offset, limit, stream, *budget)
        tail = values.get("tail", DEFAULT_TAIL)
        if tail < 1:
            raise ValueError("`tail` must be 1 or greater")
        return tail_view(reader, tail, stream, *budget)

    def _unread_or_tail(
        self, reader: SpoolView, handle: str, stream: StreamFilter, budget: tuple[int, int]
    ) -> View:
        position = self.services.spool.read_position(handle)
        if position >= reader.total_lines:
            return tail_view(reader, DEFAULT_TAIL, stream, *budget)
        view = unread_view(reader, position, stream, *budget)
        last_line = view.structured.get("last_line")
        if last_line is not None:
            self.services.spool.advance_read_position(handle, last_line)
        return view
