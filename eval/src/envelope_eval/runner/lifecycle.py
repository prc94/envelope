"""Runner lifecycle primitives shared by SSH and Docker jobs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from .planner import Job, Trial
from .results import ResultWriter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReproducibilityRecord:
    job: str
    seed: int
    suite_hash: str
    inputs: dict[str, Any]
    order: tuple[Trial, ...]

    def write(self, path: str | Path) -> None:
        payload = {
            "schema_version": 1,
            "job": self.job,
            "seed": self.seed,
            "suite_hash": self.suite_hash,
            "inputs": self.inputs,
            "order": [trial.__dict__ for trial in self.order],
        }
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@dataclass
class CircuitBreaker:
    """Trips after `limit` consecutive `infra` trials in one cell."""

    limit: int = 3
    consecutive_failures: int = 0
    tripped: bool = False
    reasons: list[str] = field(default_factory=list)
    tripped_at: float | None = None

    def stamp(self, now: float) -> None:
        """Remember when the breaker tripped (called by the runner, which owns the clock)."""
        if self.tripped and self.tripped_at is None:
            self.tripped_at = now

    def half_open(self, now: float, cooldown: float) -> bool:
        """After `cooldown` seconds let one attempt through: a single further `infra` trial trips
        the breaker again at once, a passing one closes it. A provider that was rate-limited at the
        start of a long job should not cost a model its whole experiment."""
        if not self.tripped or self.tripped_at is None or now - self.tripped_at < cooldown:
            return False
        self.tripped, self.tripped_at = False, None
        self.consecutive_failures = self.limit - 1
        self.reasons.clear()
        return True

    def observe(self, *, succeeded: bool, reason: str = "") -> None:
        if succeeded:
            self.consecutive_failures = 0
            self.reasons.clear()
            return
        self.consecutive_failures += 1
        self.reasons.append(reason)
        self.tripped = self.tripped or self.consecutive_failures >= self.limit

    def trip(self, reason: str) -> None:
        self.tripped = True
        self.reasons.append(reason)


@dataclass(frozen=True)
class GateFailure:
    """`broken`: this task cannot be solved on the target. `unavailable`: the whole cell is down."""

    status: str
    reason: str
    detail: tuple[str, ...] = ()


Cell = tuple[str, str, str, str]


def cell_of(trial: Trial) -> Cell:
    """The unit a circuit breaker covers: one model of one harness on one target and lane."""
    return (trial.lane, trial.target, trial.harness, trial.model)


class JobAborted(RuntimeError):
    """The target stayed unhealthy for longer than the job allows waiting."""


@dataclass
class TrialRunner:
    """Runs trials with retries, gates and per-cell circuit breakers; writes every record.

    `health` lists problems with the target (an empty list is healthy), for example dropped
    tunnels. It is checked before each attempt and after it: while it reports problems the job
    is paused (`restore` is tried each `poll` seconds) until it passes or `pause_timeout`
    passes, which aborts the job; an attempt after which it reports problems is `infra`.
    """

    writer: ResultWriter
    max_retries: int = 2
    breaker_limit: int = 3
    breakers: dict[Cell, CircuitBreaker] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    health: Callable[[], list[str]] | None = None
    restore: Callable[[], None] | None = None
    pause_timeout: float = 900.0
    poll: float = 5.0
    paused: float = 0.0  # seconds the job has spent paused, for the report
    cooldown: float = 900.0  # a tripped cell is probed again after this many seconds

    def await_healthy(self) -> None:
        if self.health is None:
            return
        started = self.clock()
        while problems := self.health():
            if self.clock() - started >= self.pause_timeout:
                raise JobAborted("target unhealthy, giving up: " + "; ".join(problems))
            if self.restore is not None:
                try:
                    self.restore()
                except Exception:
                    log.warning("restoring the target failed", exc_info=True)
            self.sleep(self.poll)
        self.paused += self.clock() - started

    def breaker(self, trial: Trial) -> CircuitBreaker:
        return self.breakers.setdefault(cell_of(trial), CircuitBreaker(self.breaker_limit))

    def run_trial(
        self,
        trial: Trial,
        execute: Callable[[Trial], dict[str, Any]],
        *,
        gate: Callable[[Trial], GateFailure | None] | None = None,
        persist_unavailable: bool = True,
    ) -> dict[str, Any]:
        """Run one trial. A trial of an unavailable cell is returned without being written when
        `persist_unavailable` is false, so the caller can try it again later (`write_unavailable`
        records it for good)."""
        breaker = self.breaker(trial)
        breaker.half_open(self.clock(), self.cooldown)
        if not breaker.tripped and gate is not None:
            failure = gate(trial)
            if failure is not None and failure.status == "unavailable":
                for reason in failure.detail or (failure.reason,):
                    breaker.trip(reason)
            elif failure is not None:
                return self._write(trial, status=failure.status, reason=failure.reason)
        breaker.stamp(self.clock())
        if breaker.tripped:
            record = self._record(
                trial,
                status="unavailable",
                reason="circuit breaker tripped",
                failures=list(breaker.reasons),
            )
            if persist_unavailable:
                self.writer.append(record)
            return record
        retry_reasons: list[str] = []
        for attempt in range(self.max_retries + 1):
            self.await_healthy()
            started = self.clock()
            payload = self._attempt(execute, trial)
            duration = self.clock() - started
            if self.health is not None and (problems := self.health()):
                payload = {
                    "status": "infra",
                    "attribution": "infra",
                    "passed": False,
                    "error": "target unhealthy during the trial: " + "; ".join(problems),
                }
            infra = payload.get("attribution") == "infra" and not payload.get("passed")
            if infra and attempt < self.max_retries:
                retry_reasons.append(str(payload.get("error", "")))
                continue  # a retry replaces the attempt; only its reason is kept
            breaker.observe(succeeded=not infra, reason=str(payload.get("error", "")))
            breaker.stamp(self.clock())
            return self._write(
                trial,
                **payload,
                duration=duration,
                retries=len(retry_reasons),
                retry_reasons=retry_reasons,
            )
        raise AssertionError("unreachable")

    @staticmethod
    def _attempt(execute: Callable[[Trial], dict[str, Any]], trial: Trial) -> dict[str, Any]:
        try:
            return execute(trial)
        except Exception as exc:  # noqa: BLE001 - an executor crash is not a model failure
            return {"status": "infra", "attribution": "infra", "passed": False, "error": str(exc)}

    def write_unavailable(self, record: dict[str, Any]) -> None:
        self.writer.append(record)

    def _write(self, trial: Trial, **values: Any) -> dict[str, Any]:
        record = self._record(trial, **values)
        self.writer.append(record)
        return record

    def _record(self, trial: Trial, **values: Any) -> dict[str, Any]:
        return {
            "trial": trial.index,
            "lane": trial.lane,
            "task": trial.task,
            "arm": trial.arm,
            "seed": trial.seed,
            "target": trial.target,
            "harness": trial.harness,
            "model": trial.model,
            **values,
        }


@dataclass
class Gates:
    """Oracle and canary gates run before the model trials of a cell.

    The oracle runs a task's scripted solution on the target without a model (checked once
    per target and task); a failing oracle marks the task broken there. The canary task runs
    for each harness and model (checked once per cell); if every canary attempt is `infra`,
    the cell is unavailable and its remaining trials are skipped.
    """

    oracle: Callable[[Trial], tuple[bool, str]]
    execute: Callable[[Trial], dict[str, Any]]
    canary_task: str = "canary"
    canary_trials: int = 2
    writer: ResultWriter | None = None
    cooldown: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _probed: dict[Cell, float] = field(default_factory=dict)
    _oracles: dict[tuple[str, str], GateFailure | None] = field(default_factory=dict)
    _canaries: dict[Cell, GateFailure | None] = field(default_factory=dict)

    def __call__(self, trial: Trial) -> GateFailure | None:
        cell = cell_of(trial)
        stale = self._canaries.get(cell) is not None and (
            self.clock() - self._probed.get(cell, 0.0) >= self.cooldown
        )
        if cell not in self._canaries or stale:  # a failed probe is retried after the cooldown
            self._canaries[cell] = self._run_canaries(trial)
            self._probed[cell] = self.clock()
        if self._canaries[cell] is not None:
            return self._canaries[cell]
        key = (trial.target, trial.task)
        if key not in self._oracles:
            ok, reason = self.oracle(trial)
            self._oracles[key] = None if ok else GateFailure("broken", f"oracle failed: {reason}")
        return self._oracles[key]

    def _run_canaries(self, trial: Trial) -> GateFailure | None:
        errors: list[str] = []
        for n in range(self.canary_trials):
            probe = replace(trial, index=-(n + 1), task=self.canary_task, arm=trial.arm)
            payload = TrialRunner._attempt(self.execute, probe)
            if self.writer is not None:
                self.writer.append(
                    {"gate": "canary", "lane": probe.lane, "task": probe.task}
                    | {k: v for k, v in payload.items() if k != "trial_secrets"}
                    | {"harness": probe.harness, "model": probe.model, "target": probe.target}
                )
            if payload.get("attribution") != "infra" or payload.get("passed"):
                return None
            errors.append(str(payload.get("error", "")))
        return GateFailure("unavailable", "canary trials failed for infra reasons", tuple(errors))


def reproducibility(
    job: Job, order: tuple[Trial, ...], suite_hash: str = ""
) -> ReproducibilityRecord:
    inputs = {
        "lanes": job.lanes,
        "tasks": job.tasks,
        "arms": [
            {
                "name": arm.name,
                "kind": arm.kind,
                "tags": sorted(arm.tags),
                "variant": arm.variant,
                "tool_set": list(arm.tool_set),
            }
            for arm in job.arms
        ],
        "targets": job.targets,
        "harnesses": job.harnesses,
        "models": job.models,
        "profile": job.profile,
        "wall_clock_cap": job.wall_clock_cap,
    }
    return ReproducibilityRecord(job.name, job.seed, suite_hash, inputs, order)


class Scanner(Protocol):
    def scan(self, root: str | Path) -> list[dict[str, Any]]: ...


def finish_job(results_dir: str | Path, gateway: Scanner) -> dict[str, Any]:
    """Post-job credential scan, done by the gateway (the runner never holds the key).

    Findings are file names and offsets only; any finding fails the job.
    """
    findings = gateway.scan(results_dir)
    return {"ok": not findings, "findings": findings}
