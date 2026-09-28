"""Stand-in harness that runs a task's oracle instead of a model (see launcher/oracle.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..tasks.execution import render_instruction
from .base import Trajectory
from .qwen import QwenAdapter

ORACLE_NAME = "oracle.txt"


class OracleAdapter:
    """Runs `oracle_file` through envelope on the target. The trajectory it leaves has Qwen
    Code's shape, so the parser and the tool-set check are the ones real trials use."""

    name = "oracle"

    def __init__(
        self,
        oracle_file: Path,
        *,
        python: str,
        oracle_script: str,
        envelope: str,
        shell: str | None = None,
    ) -> None:
        self.oracle_file, self.python = oracle_file, python
        self.oracle_script, self.envelope, self.shell = oracle_script, envelope, shell
        self._answer_key: Any = None

    def bind(self, answer_key: Any) -> None:
        """The trial's answer key (params only are used): an oracle may say `<<ticket>>`."""
        self._answer_key = answer_key

    def configure(self, home, model, token, tool_set, base_url, env) -> None:
        home.mkdir(parents=True, exist_ok=True)
        text = self.oracle_file.read_text(encoding="utf-8")
        text = render_instruction(text, self._answer_key)  # no-op without placeholders
        (home / ORACLE_NAME).write_text(text, encoding="utf-8")  # uploaded with the home tree

    def environment(self, model: str, token: str, base_url: str) -> dict[str, str]:
        return {}

    def implicit_tools(self, kind: str) -> set[str]:
        return set()

    def visible_tools(self, kind: str, tool_set: list[str]) -> set[str] | None:
        return None  # no model, no requests

    def command(self, instruction: str, workdir: Path, kind: str, tool_set: list[str]) -> list[str]:
        argv = [
            self.python,
            self.oracle_script,
            "--envelope",
            self.envelope,
            "--oracle-in-home",
            ORACLE_NAME,
            "--instruction",
            instruction,
        ]
        return argv + (["--shell", self.shell] if self.shell else [])

    def parse_trajectory(self, transcript: Path) -> Trajectory:
        return QwenAdapter().parse_trajectory(transcript)

    def exposed_tools(self, trajectory: Trajectory) -> set[str] | None:
        return QwenAdapter().exposed_tools(trajectory)
