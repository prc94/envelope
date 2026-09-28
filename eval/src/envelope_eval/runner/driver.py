"""Run a planned job: gates, trials, durable results, the credential scan and the report."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..report.generate import generate
from .lifecycle import GateFailure, JobAborted, TrialRunner, finish_job, reproducibility
from .planner import Job, Trial
from .results import ResultWriter

ExecutorKey = tuple[str, str, str]  # (target, harness, arm)


@dataclass
class Runtime:
    """Everything a job runs against, built from the job file (see `wiring`) or faked in tests."""

    executors: dict[ExecutorKey, Callable[[Trial], dict[str, Any]]]
    scan: Any  # has scan(directory) -> findings: the gateway, which alone knows the key
    gates: Callable[[Trial], GateFailure | None] | None = None
    health: Callable[[], list[str]] | None = None
    restore: Callable[[], None] | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    runner_options: dict[str, Any] = field(
        default_factory=dict
    )  # pause_timeout, poll, clock, sleep
    defer_poll: float = 60.0  # how often trials of an unavailable cell are tried again
    defer_timeout: float = 3600.0  # how long after the plan to keep waiting for such cells
    close: Callable[[], None] = lambda: None


@dataclass
class JobOutcome:
    ok: bool
    trials_run: int
    planned: int
    stopped: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    report_dir: Path | None = None
    paused_seconds: float = 0.0


def execute_job(
    job: Job,
    plan: tuple[Trial, ...],
    runtime: Runtime,
    out_dir: str | Path,
    suite_hash: str = "",
    *,
    task_tags: dict[str, set[str]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_record: Callable[[dict[str, Any]], None] | None = None,
) -> JobOutcome:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    record = reproducibility(job, plan, suite_hash)
    record.inputs["provenance"] = runtime.provenance  # fingerprints, variant hashes, versions
    record.write(out / "reproducibility.json")  # before the first trial

    runner = TrialRunner(
        ResultWriter(out / "results.jsonl"),
        health=runtime.health,
        restore=runtime.restore,
        **runtime.runner_options,
    )
    started, run, stopped = clock(), 0, None
    deferred: dict[int, tuple[Trial, dict[str, Any]]] = {}

    def capped() -> str | None:
        if job.wall_clock_cap is not None and clock() - started > job.wall_clock_cap:
            return f"wall-clock cap of {job.wall_clock_cap:.0f}s reached"
        return None

    def attempt(trial: Trial) -> bool:
        """Run one trial; a trial of an unavailable cell is parked, not written."""
        executor = runtime.executors.get((trial.target, trial.harness, trial.arm))
        if executor is None:
            raise LookupError(f"no executor for {(trial.target, trial.harness, trial.arm)}")
        result = runner.run_trial(trial, executor, gate=runtime.gates, persist_unavailable=False)
        if result.get("status") == "unavailable":
            deferred[trial.index] = (trial, result)
            return False
        deferred.pop(trial.index, None)
        if on_record is not None:
            on_record(result)
        return True

    try:
        for trial in plan:
            if stopped := capped():
                break
            run += attempt(trial)
        # A model whose provider was rate-limited at the start may recover: try its parked
        # trials again after the cooldown, for a bounded time, before recording them as unavailable.
        waited = 0.0
        while deferred and stopped is None and waited < runtime.defer_timeout:
            runner.sleep(runtime.defer_poll)
            waited += runtime.defer_poll
            for trial, _ in list(deferred.values()):
                if stopped := capped():
                    break
                run += attempt(trial)
    except (LookupError, JobAborted) as exc:
        stopped = str(exc)
    except KeyboardInterrupt:
        stopped = "interrupted"
    finally:
        runtime.close()
        for _, record in deferred.values():
            runner.write_unavailable(record)  # never recovered: recorded once, for the report

    # Any occurrence of the provider key in the results fails the job; the report still gets
    # written (it never contains the key), so a partial job is not lost.
    scan = finish_job(out, runtime.scan)
    summary_dir = out / "report"
    generate(
        out / "results.jsonl",
        summary_dir,
        profile=job.profile,
        rules=list(job.decision_rules),
        baselines=list(job.extra.get("baseline", [])),
        task_tags=task_tags,
    )
    return JobOutcome(
        ok=scan["ok"] and stopped is None,
        trials_run=run,
        planned=len(plan),
        stopped=stopped,
        findings=scan["findings"],
        report_dir=summary_dir,
        paused_seconds=runner.paused,
    )
