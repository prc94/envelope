"""One trial attempt from token to cleanup, against the target and gateway interfaces."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..adapters.base import Adapter, Trajectory
from ..tasks.execution import (
    Generation,
    generate_task,
    judge_task,
    render_instruction,
    workspace_integrity,
)
from ..tasks.loader import Task
from .attribution import Attribution
from .databases import DatabaseAdmin
from .gateway import GatewayLike
from .metrics import Event, lever_metrics, timeline_attribution
from .planner import Arm, Trial
from .timeline import (
    as_dicts,
    audit_events,
    has_problem,
    ledger_events,
    trajectory_tool_events,
)

log = logging.getLogger(__name__)


class InfraError(RuntimeError):
    """A setup, transport or checker failure: the attempt says nothing about the model."""


@dataclass(frozen=True)
class Workspace:
    """A trial's directories on the target; concurrent trials never share any of them."""

    workspace: str
    home: str
    state: str
    audit_log: str
    spool: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class UnitResult:
    """Outcome of running the harness inside its containment unit."""

    exit_code: int | None
    timed_out: bool
    leftovers: tuple[str, ...] = ()
    stderr: str = ""


class UnitTarget(Protocol):
    def prepare(self, trial_id: str) -> Workspace: ...
    def real_path(self, remote: str) -> str:
        """The in-unit path for one of `Workspace`'s addressing strings, for adapters that put a
        path on the harness's own command line (most targets: identity; `docker`: the container
        prefix `upload`/`download`/`run_unit` need for addressing is not a path inside the unit
        itself, so it is stripped here)."""
        ...

    def upload(self, local: Path, remote: str) -> None:
        """Copy a file or directory tree to the target, preserving bytes."""
        ...

    def download(self, remote: str, local: Path) -> None:
        """Copy a file or tree from the target; raises FileNotFoundError if it is absent."""
        ...

    def run_unit(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: str,
        timeout: float,
        stdout_to: str,
    ) -> UnitResult:
        """Run the harness in a containment unit; leftovers are listed and then killed."""
        ...

    def collect(self, script: Path, ws: Workspace, timeout: float) -> tuple[int, bytes, bytes]:
        """Run a task's collect script outside the unit, in the workspace, with UTF-8 mode."""
        ...

    def cleanup(self, ws: Workspace) -> None: ...


@contextmanager
def stage(name: str) -> Iterator[None]:
    try:
        yield
    except InfraError:
        raise
    except Exception as exc:
        raise InfraError(f"{name}: {exc}") from exc


def trial_id(job: str, trial: Trial) -> str:
    """A probe (negative index: canary and oracle gates) must be unique per *cell*
    (harness+model), not just per (job, index): `Gates._run_canaries` always probes with index
    -1/-2 regardless of which cell it is checking, and the gateway's ledger is keyed by this id
    alone (`ledger.for_trial`), so two cells sharing a job and probe index would otherwise read
    each other's tool-offering and cost data back -- harmless while every cell expected the same
    tools (a single-harness job), but a real, silent contamination once cells can expect
    different tool sets (multiple harnesses, task 14.1)."""
    if trial.index < 0:
        cell = hashlib.sha256(f"{trial.harness}:{trial.model}".encode()).hexdigest()[:8]
        return f"{job}-c{abs(trial.index):05d}-{cell}"
    return f"{job}-t{abs(trial.index):05d}"


@dataclass
class TrialExecutor:
    """Runs single attempts; `TrialRunner` adds retries, gates and the circuit breaker."""

    target: UnitTarget
    adapter: Adapter
    gateway: GatewayLike
    tasks: dict[str, Task]
    arms: dict[str, Arm]
    job: str
    gateway_url: str
    scratch: Path
    request_cap: int = 200
    auxiliary_models: dict[str, list[str]] = field(default_factory=dict)
    on_stage: Callable[[str, str], None] | None = None
    db: DatabaseAdmin | None = None

    def __call__(self, trial: Trial) -> dict[str, Any]:
        task, arm = self.tasks[trial.task], self.arms[trial.arm]
        tid = trial_id(self.job, trial)
        local = self.scratch / tid
        token: str | None = None
        try:
            local.mkdir(parents=True, exist_ok=True)
            with stage("gateway token"):
                token = self.gateway.issue(
                    lane=trial.lane,
                    job=self.job,
                    trial=tid,
                    allowed_models=[trial.model],
                    request_cap=self.request_cap,
                    auxiliary_models=self.auxiliary_models.get(trial.model, []),
                )
            result = self._attempt(trial, task, arm, tid, local, token)
            secrets = [token, *result.pop("generator_secrets", [])]
            return result | {"trial_secrets": secrets}
        except InfraError as exc:
            secrets = [token] if token else []
            return self._infra(str(exc)) | {"trial_secrets": secrets}
        finally:
            if token is not None:
                self._quietly(self.gateway.revoke, token)
            shutil.rmtree(local, ignore_errors=True)

    @staticmethod
    def _quietly(action: Callable[[Any], Any], value: Any) -> None:
        try:
            action(value)
        except Exception:
            log.warning("cleanup step failed", exc_info=True)

    @staticmethod
    def _infra(reason: str) -> dict[str, Any]:
        return {"status": "infra", "attribution": "infra", "passed": False, "error": reason}

    def _note(self, tid: str, what: str) -> None:
        if self.on_stage:
            self.on_stage(tid, what)

    def _attempt(
        self, trial: Trial, task: Task, arm: Arm, tid: str, local: Path, token: str
    ) -> dict[str, Any]:
        with stage("prepare target"):
            ws = self.target.prepare(tid)
        holder: dict[str, Any] = {}
        try:
            return self._run(trial, task, arm, tid, local, token, ws, holder)
        finally:
            self._quietly(self.target.cleanup, ws)
            if "generation" in holder:
                self._cleanup_db(holder["generation"])

    def _cleanup_db(self, generation: Generation) -> None:
        """Delete whatever `redis_user`/`mongo_user` the generator provisioned (task 12.1),
        declared in the answer key as `{"db_cleanup": [{"kind": "redis"|"mongo", "prefix": ...}]}`.
        Runs even when the trial failed or errored, same as target cleanup."""
        if self.db is None or not isinstance(generation.answer_key, dict):
            return
        for entry in generation.answer_key.get("db_cleanup", []):
            kind, prefix = entry.get("kind"), entry.get("prefix")
            if kind == "redis" and prefix:
                self._quietly(self.db.redis_delete, prefix)
            elif kind == "mongo" and prefix:
                self._quietly(self.db.mongo_delete, prefix)

    def _run(
        self,
        trial: Trial,
        task: Task,
        arm: Arm,
        tid: str,
        local: Path,
        token: str,
        ws: Workspace,
        holder: dict[str, Any],
    ) -> dict[str, Any]:
        self._note(tid, "generate")
        with stage("generate fixtures"):
            generation = generate_task(
                task.root, local / "fixtures", trial.seed, prefix=tid, db=self.db
            )
        holder["generation"] = generation
        with stage("upload fixtures"):
            self.target.upload(generation.fixture_dir, ws.workspace)
        bind = getattr(self.adapter, "bind", None)
        if bind is not None:
            bind(generation.answer_key)  # oracles fill their own <<placeholders>> from the params
        with stage("render instruction"):
            instruction = render_instruction(task.instruction, generation.answer_key)
        env = {**ws.env, **arm.settings}  # the arm's envelope settings reach envelope via its env
        with stage("configure harness"):
            staged_home = local / "home"
            self.adapter.configure(
                staged_home, trial.model, token, list(arm.tool_set), self.gateway_url, env
            )
            self.target.upload(staged_home, ws.home)

        self._confine(ws, generation.answer_key)

        self._note(tid, "agent")
        transcript_remote = ws.state + "/transcript.jsonl"
        with stage("run harness"):
            workdir = self.target.real_path(ws.workspace)
            unit = self.target.run_unit(
                self.adapter.command(instruction, Path(workdir), arm.kind, list(arm.tool_set)),
                env=env | self.adapter.environment(trial.model, token, self.gateway_url),
                cwd=ws.workspace,
                timeout=task.timeout,
                stdout_to=transcript_remote,
            )
        gateway_state = self._state(token)
        ledger = self.gateway.ledger(tid)
        self._check_offered_tools(ledger, arm)

        with stage("fetch transcript"):
            trajectory = self._trajectory(local, transcript_remote)
        exposed = self.adapter.exposed_tools(trajectory)
        expected = set(arm.tool_set) | self.adapter.implicit_tools(arm.kind)
        # Harnesses that report no tool list at start-up (OpenCode) are checked against the tool
        # names the gateway saw instead (`_check_offered_tools`).
        if getattr(self.adapter, "reports_tools", True) and exposed != expected:
            detail = (
                "no report"
                if exposed is None
                else (
                    f"missing {sorted(expected - exposed)}, unexpected {sorted(exposed - expected)}"
                )
            )
            raise InfraError(f"tool set mismatch ({detail}); trial not scored")

        self._note(tid, "collect")
        with stage("collect"):
            code, out, err = self.target.collect(task.root / "collect.py", ws, 120)
            if code:
                raise RuntimeError(f"collect exited {code}: {err.decode('utf-8', 'replace')[:500]}")
            observations = json.loads(out)
        with stage("fetch workspace"):
            after = local / "after"
            self.target.download(ws.workspace, after)
            integrity = workspace_integrity(generation.manifest, after, set(task.modifiable_paths))
        with stage("judge"):
            verdict = judge_task(
                task.root, observations, generation.answer_key, trajectory.final_answer
            )
        with stage("fetch audit log"):
            audit_rows = self._audit_rows(local, ws.audit_log) if arm.kind == "envelope" else []

        events = self._timeline(ledger, gateway_state, audit_rows, trajectory, unit, arm)
        metrics = self._metrics(ledger, audit_rows, trajectory, unit, integrity["created"])
        passed = bool(verdict["passed"]) and integrity["ok"] and not unit.timed_out
        attribution: Attribution | None = None
        if not passed:
            attribution = timeline_attribution(events)
            if attribution == "indeterminate" and not has_problem(events):
                attribution = "model"  # ran to completion without any fault: a wrong result
            if integrity["ok"] is False and verdict["passed"]:
                attribution = "model"
        main = [row for row in ledger if row.get("role") == "main" and _completed(row)]
        failed = [row for row in ledger if row.get("role") == "main" and not _completed(row)]
        return {
            "status": "pass" if passed else "fail",
            "passed": passed,
            "attribution": attribution,
            "judge": {k: v for k, v in verdict.items() if k != "passed"},
            "integrity": integrity,
            "leftover_processes": list(unit.leftovers),
            "exit_code": unit.exit_code,
            "timed_out": unit.timed_out,
            "turns": len(main),
            "providers": sorted({str(row["provider"]) for row in ledger if row.get("provider")}),
            "failed_requests": len(failed),  # provider errors the harness saw (429s, 5xx)
            "absorbed_retries": sum(len(row.get("retried", [])) for row in ledger),
            "tokens": sum(_tokens(row) for row in main),
            "cost": sum(_cost(row) for row in ledger),
            "tool_set": sorted(expected),
            "tool_sequence": [
                str(e.get("name")) for e in trajectory.events if e.get("type") == "tool_call"
            ],
            "transcript_truncated": trajectory.truncated,
            "timeline": as_dicts(events),
            "known_gaps": list(task.known_gaps),
            "generator_secrets": (
                list(generation.answer_key.get("secrets", []))
                + list(generation.answer_key.get("redact", []))
                if isinstance(generation.answer_key, dict)
                else []
            ),
            **metrics,
        }

    def _check_offered_tools(self, ledger: list[dict[str, Any]], arm: Arm) -> None:
        """The harness's own report says what is configured; the gateway saw what the model was
        actually offered. A tool outside the arm's set (a harness update re-enabling one, a
        background agent with its own tools) makes the trial unscoreable."""
        allowed = self.adapter.visible_tools(arm.kind, list(arm.tool_set))
        if allowed is None:
            return
        extra = sorted(
            {
                name
                for row in ledger
                if row.get("role") == "main"
                for name in row.get("tool_names", [])
            }
            - allowed
        )
        if extra:
            raise InfraError(f"model was offered tools outside the configured set: {extra}")

    def _confine(self, ws: Workspace, answer_key: Any) -> None:
        """Fail the attempt if a secret the generator declared (`{"secrets": [...]}` in its
        answer key: answers, seeds) is already reachable on the target, where the agent could
        read it. Targets that cannot search (no `find_secrets`) skip the check.

        A generator that legitimately hands the agent a credential (a database task's
        connection details, task 13.1) declares it under `{"redact": [...]}` instead: still
        folded into `generator_secrets`/`trial_secrets` for report redaction, but never checked
        here, since it is expected to be reachable."""
        secrets = answer_key.get("secrets") if isinstance(answer_key, dict) else None
        search = getattr(self.target, "find_secrets", None)
        if not secrets or search is None:
            return
        with stage("confinement check"):
            hits = search(ws, [str(x) for x in secrets])
        if hits:
            raise InfraError(f"answer key reachable on the target: {', '.join(hits)}")

    def _state(self, token: str) -> dict[str, Any]:
        try:
            return self.gateway.state(token)
        except Exception:  # noqa: BLE001
            return {}

    def _trajectory(self, local: Path, remote: str) -> Trajectory:
        target = local / "transcript.jsonl"
        try:
            self.target.download(remote, target)
        except FileNotFoundError:
            return Trajectory((), "", True)
        return self.adapter.parse_trajectory(target)

    def _audit_rows(self, local: Path, remote: str) -> list[dict[str, Any]]:
        target = local / "audit.jsonl"
        try:
            self.target.download(remote, target)
        except FileNotFoundError:
            return []
        rows = []
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    @staticmethod
    def _timeline(
        ledger: list[dict[str, Any]],
        gateway_state: dict[str, Any],
        audit_rows: list[dict[str, Any]],
        trajectory: Trajectory,
        unit: UnitResult,
        arm: Arm,
    ) -> list[Event]:
        events = ledger_events(ledger, in_flight=bool(gateway_state.get("in_flight")))
        if arm.kind == "envelope":
            tool_events, _ = audit_events(audit_rows)
        else:
            tool_events = trajectory_tool_events(list(trajectory.events))
        events += tool_events
        if unit.timed_out:
            events.append(Event(float("inf"), "timeout", "harness", {}))
        elif unit.exit_code not in (0, None) and not trajectory.final_answer:
            events.append(Event(float("inf"), "harness_error", "harness", {"exit": unit.exit_code}))
        return sorted(events, key=lambda e: e.timestamp)

    @staticmethod
    def _metrics(
        ledger: list[dict[str, Any]],
        audit_rows: list[dict[str, Any]],
        trajectory: Trajectory,
        unit: UnitResult,
        created: list[str],
    ) -> dict[str, Any]:
        _, audit_metric_events = audit_events(audit_rows)
        model_requests = [
            {
                "kind": "model_request",
                "auxiliary": row.get("role") == "auxiliary",
                "tokens": _tokens(row),
            }
            for row in ledger
            if _completed(row)  # a rate-limited request is not a model turn
        ]
        leftovers = [{"kind": "leftover"} for _ in unit.leftovers]
        return lever_metrics(
            model_requests + audit_metric_events + leftovers, list(trajectory.events)
        )


def _completed(row: dict[str, Any]) -> bool:
    return row.get("end_reason") == "complete"


def _tokens(row: dict[str, Any]) -> int:
    usage = row.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def _cost(row: dict[str, Any]) -> float:
    return float((row.get("usage") or {}).get("cost") or 0)
