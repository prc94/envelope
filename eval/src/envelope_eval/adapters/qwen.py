"""Qwen Code (spike 1.3, pinned 0.24.5).

What the spike established, and how it is used here:
* `-o stream-json` is the incremental, kill-tolerant transcript; the process exit code is not
  reliable on Windows, so trials are scored from the `result` event and the final state.
* `--exclude-tools` removes tools from both the init report and the model's requests, so every
  built-in tool the arm does not list is excluded. `--core-tools` is not an allowlist.
* Outside `--bare`, MCP tools are deferred behind `tool_search` and `tool_call`, which is how a
  user meets them; the adapter runs non-bare, so those two are implicit in envelope arms.
* A background memory agent otherwise makes requests with its own edit/shell tools; the
  `memory.*` settings switch it off.
* On Windows the `qwen.cmd` shim only runs `node cli-entry.js`, so the job can give the node
  command directly (`launch`) and avoid `cmd` parsing of a multi-line prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import Trajectory

# Built-in tools of Qwen Code 0.24.5 as reported by its init event with no exclusions.
BUILTIN_TOOLS = (
    "read_mcp_resource",
    "skill",
    "grep_search",
    "glob",
    "cron_create",
    "cron_list",
    "cron_delete",
    "task_stop",
    "read_file",
    "zoom_image",
    "notebook_edit",
    "enter_worktree",
    "exit_worktree",
    "record_artifact",
    "loop_wakeup",
    "get_goal",
    "update_goal",
    "agent",
    "list_agents",
    "send_message",
    "write_file",
    "run_shell_command",
    "report_findings",
    "monitor",
    "web_fetch",
    "tool_search",
    "tool_call",
    "edit",
)
DEFERRAL = {"tool_search", "tool_call"}  # how deferred MCP tools are reached
MCP_PREFIX = "mcp__envelope__"


class QwenAdapter:
    name = "qwen-code"

    def __init__(
        self,
        launch: list[str] | None = None,
        envelope_command: list[str] | None = None,
    ) -> None:
        """`launch` is the command that starts Qwen Code (default `qwen`; on Windows the node
        executable and `cli-entry.js`); `envelope_command` starts envelope's MCP server."""
        self.launch = launch or ["qwen"]
        self.envelope_command = envelope_command or ["python", "envelope.pyz"]

    def _uses_envelope(self, kind: str) -> bool:
        return kind == "envelope"

    def implicit_tools(self, kind: str) -> set[str]:
        return set(DEFERRAL) if self._uses_envelope(kind) else set()

    def visible_tools(self, kind: str, tool_set: list[str]) -> set[str] | None:
        own = {t for t in tool_set if not t.startswith("mcp__")}
        return own | self.implicit_tools(kind)

    def _excluded(self, kind: str, tool_set: list[str]) -> list[str]:
        keep = set(tool_set) | self.implicit_tools(kind)
        return [tool for tool in BUILTIN_TOOLS if tool not in keep]

    def configure(
        self,
        home: Path,
        model: str,
        token: str,
        tool_set: list[str],
        base_url: str = "http://127.0.0.1/v1",
        env: dict[str, str] | None = None,
    ) -> None:
        settings: dict[str, object] = {
            "security": {"auth": {"selectedType": "openai"}},
            "memory": {"enableManagedAutoMemory": False, "enableManagedAutoDream": False},
        }
        if any(t.startswith(MCP_PREFIX) for t in tool_set):
            settings["mcpServers"] = {
                "envelope": {
                    "command": self.envelope_command[0],
                    "args": self.envelope_command[1:],
                    "env": dict(env or {}),
                }
            }
        (home / ".qwen").mkdir(parents=True, exist_ok=True)
        (home / ".qwen" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def environment(self, model: str, token: str, base_url: str) -> dict[str, str]:
        return {
            "OPENAI_BASE_URL": base_url,
            "OPENAI_API_KEY": token,
            "OPENAI_MODEL": model,
            "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
        }

    def command(self, instruction: str, workdir: Path, kind: str, tool_set: list[str]) -> list[str]:
        argv = [*self.launch, "--auth-type", "openai", "-y", "-o", "stream-json"]
        excluded = self._excluded(kind, tool_set)
        if excluded:
            argv += ["--exclude-tools", *excluded]
        return [*argv, "-p", instruction]  # the prompt last: --exclude-tools takes a list

    def parse_trajectory(self, transcript: Path) -> Trajectory:
        from .trajectory import parse_transcript

        return parse_transcript(transcript)

    def exposed_tools(self, trajectory: Trajectory) -> set[str] | None:
        for event in trajectory.events:
            if event.get("type") == "system" and event.get("subtype") == "init":
                tools = event.get("tools")
                return {str(t) for t in tools} if isinstance(tools, list) else None
        return None
