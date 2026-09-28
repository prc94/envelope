"""Oracle gate: run a task's scripted solution on a target, with no model, and judge it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters.oracle import OracleAdapter
from ..tasks.loader import Task
from .planner import Arm, Trial
from .trial import TrialExecutor, UnitTarget

ENVELOPE_TOOLS = ("mcp__envelope__run", "mcp__envelope__output")


class NullGateway:
    """An oracle makes no model requests, so it needs no token and leaves no ledger."""

    def issue(self, **_: Any) -> str:
        return "oracle"

    def revoke(self, token: str) -> None:
        pass

    def state(self, token: str) -> dict[str, Any]:
        return {"in_flight": False}

    def ledger(self, trial: str) -> list[dict[str, Any]]:
        return []


@dataclass
class OracleRunner:
    """Runs oracles on one target. `python`, `oracle_script` and `envelope` are paths on it."""

    target: UnitTarget
    tasks: dict[str, Task]
    scratch: Path
    python: str
    oracle_script: str
    envelope: str
    shell: str | None = None
    job: str = "oracle"
    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    def run(self, task_id: str, seed: int = 1) -> dict[str, Any]:
        task = self.tasks[task_id]
        adapter = OracleAdapter(
            task.root / "oracle",
            python=self.python,
            oracle_script=self.oracle_script,
            envelope=self.envelope,
            shell=self.shell,
        )
        executor = TrialExecutor(
            self.target,
            adapter,
            NullGateway(),
            {task_id: task},
            {"oracle": Arm("oracle", tool_set=ENVELOPE_TOOLS)},
            job=self.job,
            gateway_url="",
            scratch=self.scratch,
        )
        trial = Trial(0, "", task_id, "oracle", seed, "", "oracle", "")
        record = executor(trial)
        self.results[task_id] = record
        return record

    def check(self) -> Callable[[Trial], tuple[bool, str]]:
        """A `Gates.oracle`: (passed, reason), run once per task by the gate's own cache."""

        def gate(trial: Trial) -> tuple[bool, str]:
            record = self.run(trial.task, trial.seed)
            if record.get("passed"):
                return True, ""
            reason = (record.get("judge") or {}).get("reason") or record.get("error")
            return False, str(reason or record.get("attribution") or record.get("status"))

        return gate
