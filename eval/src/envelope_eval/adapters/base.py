from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Trajectory:
    events: tuple[dict[str, Any], ...]
    final_answer: str = ""
    truncated: bool = False


class Adapter(Protocol):
    """One harness. `kind` is the arm kind (`envelope` or `native`) and `tool_set` the arm's own
    tool names; the adapter adds the tools its harness needs to reach them (`implicit_tools`)."""

    name: str

    def configure(
        self,
        home: Path,
        model: str,
        token: str,
        tool_set: list[str],
        base_url: str,
        env: dict[str, str],
    ) -> None:
        """Write the harness's user-level configuration into `home` (a local staging copy).
        `env` is the trial's environment on the target (its directories, ENVELOPE_* keys and
        the arm's settings), for the envelope MCP server entry."""
        ...

    def environment(self, model: str, token: str, base_url: str) -> dict[str, str]:
        """Environment variables the harness process needs (model, gateway URL and token)."""
        ...

    def command(self, instruction: str, workdir: Path, kind: str, tool_set: list[str]) -> list[str]:
        """Headless argv. The harness's stdout is captured as the transcript."""
        ...

    def parse_trajectory(self, transcript: Path) -> Trajectory: ...

    def exposed_tools(self, trajectory: Trajectory) -> set[str] | None:
        """The tools the harness itself reports as configured, or None if it reported none."""
        ...

    def implicit_tools(self, kind: str) -> set[str]:
        """Tools the harness adds to an arm's tool set (Qwen Code's deferred-tool wrappers)."""
        ...

    def visible_tools(self, kind: str, tool_set: list[str]) -> set[str] | None:
        """What the model may be offered in a request, or None when not checkable. The gateway
        ledger records the offered names; anything outside this set makes the trial `infra`."""
        ...
