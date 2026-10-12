"""`python -m envelope_eval`: plan, run and report evaluation jobs."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

from .calibration import compare, render, run_calibration
from .report.generate import generate
from .runner.driver import execute_job
from .runner.planner import Job, Trial, load_job, plan_trials
from .runner.wiring import build_runtime, expand_tasks
from .targets import SSHTarget, WindowsTrialTarget
from .tasks import TaskSuite, load_suite

REPO = Path(__file__).resolve().parents[3]
SECONDS_PER_TRIAL = 180  # design E12: about three minutes per serial Windows trial


def _load(job_file: str, tasks_dir: str) -> tuple[Job, TaskSuite]:
    suite = load_suite(tasks_dir)
    if suite.errors:
        raise SystemExit("task suite errors:\n  " + "\n  ".join(suite.errors))
    job = load_job(job_file)
    job = dataclasses.replace(job, tasks=expand_tasks(job.tasks, suite, job.lanes))
    return job, suite


def _plan(job: Job, suite: TaskSuite) -> tuple[Trial, ...]:
    tags = {t.id: set(t.tags) for t in suite.tasks}
    baseline = {t.id: t.baseline_eligible for t in suite.tasks}
    return plan_trials(job, tags, baseline)


def cmd_plan(args: argparse.Namespace) -> int:
    job, suite = _load(args.job, args.tasks)
    plan = _plan(job, suite)
    hours = len(plan) * SECONDS_PER_TRIAL / 3600
    print(
        f"job {job.name}: {len(plan)} trials over {len(job.tasks)} tasks, {len(job.arms)} arms, "
        f"{len(job.models)} models, {job.trials} per cell"
    )
    print(
        f"about {hours:.1f} h at {SECONDS_PER_TRIAL // 60} min per serial trial; "
        f"wall-clock cap {job.wall_clock_cap or 'none'} s"
    )
    print("tasks:", ", ".join(job.tasks))
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    suite = load_suite(args.tasks)
    if suite.errors:
        raise SystemExit("task suite errors:\n  " + "\n  ".join(suite.errors))
    print(suite.coverage_report())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    job, suite = _load(args.job, args.tasks)
    plan = _plan(job, suite)
    out = Path(
        args.out or REPO / "eval" / "results" / f"{job.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    print(f"{len(plan)} trials -> {out}")
    runtime = build_runtime(job, suite, REPO, out, log=lambda m: print(m, file=sys.stderr))
    outcome = execute_job(
        job,
        plan,
        runtime,
        out,
        suite.hash,
        task_tags={t.id: set(t.tags) for t in suite.tasks},
        on_record=lambda r: print(
            f"[{r.get('trial')}] {r.get('task')} {r.get('arm')} {r.get('model')}: "
            f"{r.get('status')}",
            file=sys.stderr,
        ),
    )
    print(f"ran {outcome.trials_run}/{outcome.planned}; report in {outcome.report_dir}")
    if outcome.findings:
        print(f"CREDENTIAL FOUND in results ({len(outcome.findings)} occurrence(s)): job failed")
    if outcome.stopped:
        print(f"stopped: {outcome.stopped}")
    return 0 if outcome.ok else 2


def cmd_report(args: argparse.Namespace) -> int:
    results = Path(args.results)
    generate(results / "results.jsonl", results / "report", profile=args.profile)
    print(results / "report" / "report.md")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Compare native tool bytes from SSH and from the interactive session (needs a logged-on user)."""
    ssh = SSHTarget(args.host, args.user)
    WindowsTrialTarget(ssh).setup()  # makes sure the launcher is installed
    captured = run_calibration(ssh)
    result = compare(*captured)
    print(render(result))
    return 0 if result.same else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envelope_eval", description=__doc__)
    parser.add_argument("--tasks", default=str(REPO / "eval" / "tasks"))
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="show what a job would run")
    plan.add_argument("job")
    plan.set_defaults(func=cmd_plan)
    coverage = sub.add_parser("coverage", help="show required task areas by lane")
    coverage.set_defaults(func=cmd_coverage)
    run = sub.add_parser("run", help="run a job")
    run.add_argument("job")
    run.add_argument("--out")
    run.set_defaults(func=cmd_run)
    report = sub.add_parser("report", help="regenerate the report of a results directory")
    report.add_argument("results")
    report.add_argument("--profile", default="screening")
    report.set_defaults(func=cmd_report)
    calibrate = sub.add_parser("calibrate", help="compare SSH and interactive console output (E8)")
    calibrate.add_argument("--host", default="192.168.94.172")
    calibrate.add_argument("--user", default="envelope")
    calibrate.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
