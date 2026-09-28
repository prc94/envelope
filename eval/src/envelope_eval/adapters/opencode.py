"""OpenCode (`opencode run --format json`), on the Docker lane.

Design E4: a custom OpenAI-compatible provider pointing at the gateway (the token travels in the
environment and is referenced from the config as `{env:...}`, so it is never written to a file),
`small_model` set to the model under test so OpenCode's title agent does not pick a model of its
own (it would be an auxiliary request, and outside the token's allowed models), a per-trial `HOME`
for its config and data, `--dir` for the workspace, and every built-in tool but the arm's shell tool
switched off.

Unlike Qwen Code, OpenCode reports no tool list at start-up, so the tool-set check rests on the tool
names the gateway saw in each request (`reports_tools = False`).

The config schema follows OpenCode's documentation; it has not been exercised against a real OpenCode
yet (task 10.3's live verification needs the Linux image).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Trajectory

PROVIDER = "gateway"
TOKEN_ENV = "EVAL_GATEWAY_TOKEN"
BUILTIN_TOOLS = (
    "bash",
    "edit",
    "write",
    "read",
    "grep",
    "glob",
    "list",
    "patch",
    "todowrite",
    "todoread",
    "webfetch",
    "websearch",
    "task",
    "skill",
    "lsp",
)
MCP_SERVER = "envelope"


class OpenCodeAdapter:
    name = "opencode"
    reports_tools = False

    def __init__(
        self, launch: list[str] | None = None, envelope_command: list[str] | None = None
    ) -> None:
        self.launch = launch or ["opencode"]
        self.envelope_command = envelope_command or ["python", "envelope.pyz"]
        self._model = ""

    def implicit_tools(self, kind: str) -> set[str]:
        return set()

    def visible_tools(self, kind: str, tool_set: list[str]) -> set[str] | None:
        return set(tool_set)

    def _permissions(self, tool_set: list[str]) -> dict[str, str]:
        return {
            tool: ("allow" if tool in tool_set else "deny") for tool in ("bash", "edit", "webfetch")
        }

    def configure(
        self,
        home: Path,
        model: str,
        token: str,
        tool_set: list[str],
        base_url: str,
        env: dict[str, str] | None = None,
    ) -> None:
        self._model = model
        ref = f"{PROVIDER}/{model}"
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "model": ref,
            "small_model": ref,  # the title agent otherwise chooses its own model
            "autoupdate": False,
            "share": "disabled",
            "provider": {
                PROVIDER: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Evaluation gateway",
                    "options": {"baseURL": base_url, "apiKey": f"{{env:{TOKEN_ENV}}}"},
                    "models": {model: {"name": model}},
                }
            },
            "tools": {tool: tool in tool_set for tool in BUILTIN_TOOLS},
            "permission": self._permissions(tool_set),
        }
        if any(t.startswith(f"{MCP_SERVER}_") for t in tool_set):
            config["mcp"] = {
                MCP_SERVER: {
                    "type": "local",
                    "command": list(self.envelope_command),
                    "enabled": True,
                    "environment": dict(env or {}),
                }
            }
        target = home / ".config" / "opencode"
        target.mkdir(parents=True, exist_ok=True)
        (target / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    def environment(self, model: str, token: str, base_url: str) -> dict[str, str]:
        return {TOKEN_ENV: token, "OPENCODE_DISABLE_AUTOUPDATE": "1"}

    def command(self, instruction: str, workdir: Path, kind: str, tool_set: list[str]) -> list[str]:
        return [
            *self.launch,
            "run",
            "--pure",
            "--format",
            "json",
            "--dir",
            str(workdir),
            "-m",
            f"{PROVIDER}/{self._model}",
            instruction,
        ]

    def parse_trajectory(self, transcript: Path) -> Trajectory:
        """Events of `--format json`: `tool_use` parts carry the call and its result together, `text`
        parts the model's words. A run killed mid-line leaves a cut last line, which is dropped."""
        text = transcript.read_text(encoding="utf-8", errors="replace")
        events: list[dict[str, Any]] = []
        truncated = False
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                truncated = True
                break
            part = item.get("part") or {}
            kind = item.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                events.append({"type": "text", "role": "assistant", "content": part["text"]})
            elif kind == "tool_use":
                events += _tool_events(part)
        final = next(
            (e["content"] for e in reversed(events) if e["type"] == "text"),
            "",
        )
        return Trajectory(tuple(events), final, truncated)

    def exposed_tools(self, trajectory: Trajectory) -> set[str] | None:
        return None


def _tool_events(part: dict[str, Any]) -> list[dict[str, Any]]:
    state = part.get("state") or {}
    raw = str(part.get("tool", ""))
    call = {
        "type": "tool_call",
        "id": part.get("callID"),
        "raw_name": raw,
        "name": raw.removeprefix(f"{MCP_SERVER}_"),  # envelope_run -> run; bash stays bash
        "arguments": state.get("input") or {},
    }
    events = [call]
    if state.get("status") in ("completed", "error"):
        events.append(
            {
                "type": "tool_result",
                "id": part.get("callID"),
                "is_error": state.get("status") == "error",
                "content": str(state.get("output", state.get("error", ""))),
            }
        )
    return events
