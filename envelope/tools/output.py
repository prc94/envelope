"""The ``output`` tool: page, tail or grep the spooled output of a previous run."""

from __future__ import annotations

import asyncio
from typing import Any

from envelope.audit import call_record
from envelope.output.spool import HandleError
from envelope.output.view import (
    DEFAULT_LIMIT,
    DEFAULT_TAIL,
    View,
    ViewError,
    grep_view,
    range_view,
    tail_view,
)
from envelope.protocol.server import CallContext, ToolResult
from envelope.services import Services
from envelope.tools.coercion import ArgumentError, Param, coerce

PARAMS = (
    Param("handle", "string", "must be an output handle like out_1a2b3c4d (a string)"),
    Param("tail", "integer", "must be a whole number of lines"),
    Param("offset", "integer", "must be a 1-based line number"),
    Param("limit", "integer", "must be a whole number of lines"),
    Param("grep", "string", "must be a regular expression (a string)"),
    Param("context", "integer", "must be a whole number of context lines"),
    Param("stream", "choice", "must be one of both, stdout, stderr", ("both", "stdout", "stderr")),
)


def output_definition() -> dict[str, Any]:
    return {
        "name": "output",
        "title": "Read command output",
        "description": (
            "Read the full, saved output of an earlier `run` shell command by its handle (out_...), "
            "for example when the terminal output shown in the run result was truncated. Choose ONE "
            f"mode: tail (last N lines, the default with {DEFAULT_TAIL} lines), offset+limit (1-based "
            f"line range, limit defaults to {DEFAULT_LIMIT}), or grep (Python regular expression) with "
            "optional context lines. stream selects stdout, stderr or both (default). Lines are "
            "returned with their line numbers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Output handle from a run result, e.g. out_91c2ab04.",
                },
                "tail": {"type": "integer", "description": "Return the last N lines."},
                "offset": {"type": "integer", "description": "First line to return (1-based)."},
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to return from offset.",
                },
                "grep": {
                    "type": "string",
                    "description": "Regular expression; returns matching lines.",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context around each grep match.",
                },
                "stream": {
                    "type": "string",
                    "enum": ["both", "stdout", "stderr"],
                    "description": "Which stream to read (default both).",
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
        coercions: list[dict[str, Any]] = list(ctx.extra.get("coercions", []))
        try:
            values, more = coerce(arguments, PARAMS)
            coercions.extend(more)
            view = await asyncio.to_thread(self._view, values)
        except (ArgumentError, HandleError, ViewError, ValueError) as exc:
            self._audit(ctx, arguments, coercions, {"error": str(exc)})
            return ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
        self._audit(
            ctx,
            values,
            coercions,
            {k: view.structured.get(k) for k in ("handle", "mode", "truncated")},
        )
        return ToolResult(view.text, view.structured)

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
        mode = modes[0] if modes else "tail"
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
