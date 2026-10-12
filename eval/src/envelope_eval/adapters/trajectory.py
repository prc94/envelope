"""Normalize harness transcripts into a small ATIF-compatible event stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Trajectory


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "event" in value and isinstance(value["event"], dict):
        return _unwrap(value["event"])
    if isinstance(value, dict) and value.get("type") == "tool_call" and "tool_call" in value:
        return _unwrap(value["tool_call"])
    return value


SHELL_PREFIX = "mcp__envelope__"


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return "" if content is None else str(content)


def _tool_call(block: dict[str, Any]) -> dict[str, Any]:
    """A `tool_use` block. Qwen Code hides MCP tools behind a generic `tool_call {name,
    arguments}` wrapper (deferred tools); it is unwrapped to the tool actually called, and
    envelope's tool names lose their `mcp__envelope__` prefix."""
    name, args = str(block.get("name", "")), block.get("input") or {}
    call: dict[str, Any] = {"type": "tool_call", "id": block.get("id")}
    if name == "tool_call" and isinstance(args, dict) and isinstance(args.get("name"), str):
        call["via"] = "tool_call"
        name, args = args["name"], args.get("arguments") or {}
    call["raw_name"] = name
    call["name"] = name.removeprefix(SHELL_PREFIX)
    call["arguments"] = args
    return call


def _expand(item: Any) -> list[dict[str, Any]]:
    """Events of one transcript line. Qwen's `stream-json` nests tool calls and results as
    content blocks of `assistant`/`user` messages; flat and wrapped events (S5) pass through."""
    if not isinstance(item, dict):
        return []
    kind = item.get("type")
    message = item.get("message")
    if kind in ("assistant", "user") and isinstance(message, dict):
        out: list[dict[str, Any]] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                out.append({"type": "text", "role": kind, "content": block.get("text", "")})
            elif block.get("type") == "tool_use":
                out.append(_tool_call(block))
            elif block.get("type") == "tool_result":
                out.append(
                    {
                        "type": "tool_result",
                        "id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": _text(block.get("content")),
                    }
                )
        return out
    if kind == "stream_event":
        return []  # partial-message deltas; the complete messages carry the same content
    if kind == "result":
        return [
            {
                "type": "result",
                "subtype": item.get("subtype"),
                "is_error": bool(item.get("is_error")),
                "content": _text(item.get("result")),
                "num_turns": item.get("num_turns"),
                "usage": item.get("usage"),
            }
        ]
    unwrapped = _unwrap(item)
    return [unwrapped] if isinstance(unwrapped, dict) else []


def parse_transcript(path: str | Path) -> Trajectory:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    truncated = False
    try:
        value = json.loads(text)
        raw = (
            value
            if isinstance(value, list)
            else value.get("events", value.get("messages", []))
            if isinstance(value, dict)
            else []
        )
    except json.JSONDecodeError:
        raw = []
        for line in text.splitlines():
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError:
                # Some harnesses write schema warnings to stdout between valid JSONL events.
                # A partial JSON event still means the stream was cut off.
                if line.lstrip().startswith(("{", "[")):
                    truncated = True
                    break
    events = tuple(event for item in raw for event in _expand(item))
    final = ""
    for event in reversed(events):
        if event.get("role") == "assistant" and isinstance(event.get("content"), str):
            final = event["content"]
            break
        if event.get("type") in {"final", "result"}:
            final = str(event.get("content", event.get("text", "")))
            break
    return Trajectory(events, final, truncated)
