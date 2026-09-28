"""The job engine with fake executors: ordering, gates, the wall-clock cap, aborts and the scan."""

import json
from pathlib import Path

from envelope_eval.runner.driver import Runtime, execute_job
from envelope_eval.runner.lifecycle import GateFailure, Gates
from envelope_eval.runner.planner import Arm, Job, plan_trials
from envelope_eval.runner.results import read_results


class Scanner:
    def __init__(self, findings=()):
        self.findings, self.roots = list(findings), []

    def scan(self, root):
        self.roots.append(str(root))
        return self.findings


def make_job(
    tasks: tuple[str, ...] = ("a", "b"),
    models: tuple[str, ...] = ("m1", "m2"),
    arms: tuple[Arm, ...] = (Arm("default"), Arm("budget")),
    trials: int = 2,
    wall_clock_cap: float | None = None,
) -> Job:
    return Job(
        name="job",
        seed=3,
        lanes=("windows",),
        tasks=tasks,
        arms=arms,
        trials=trials,
        profile="screening",
        wall_clock_cap=wall_clock_cap,
        targets=("vm",),
        harnesses=("qwen",),
        models=models,
    )


def passing(trial):
    return {"status": "pass", "passed": True, "tokens": 100, "turns": 2}


def runtime(execute=passing, **kw):
    keys = [("vm", "qwen", "default"), ("vm", "qwen", "budget")]
    kw.setdefault("runner_options", {"sleep": lambda seconds: None})  # never really wait in tests
    return Runtime({k: execute for k in keys}, Scanner(), **kw)


def test_runs_the_whole_plan_and_writes_results_report_and_the_record_first(tmp_path):
    job = make_job()
    plan = plan_trials(job)
    seen_when_first_trial_ran: dict[str, bool] = {}

    def execute(trial):
        seen_when_first_trial_ran.setdefault(
            "repro_written", (tmp_path / "reproducibility.json").exists()
        )
        return passing(trial)

    rt = runtime(execute, provenance={"fingerprint": {"vm": "abc"}})
    outcome = execute_job(job, plan, rt, tmp_path, "suite-hash")
    assert outcome.ok and outcome.trials_run == outcome.planned == len(plan) == 16
    assert seen_when_first_trial_ran["repro_written"] is True  # recorded before the first trial
    repro = json.loads((tmp_path / "reproducibility.json").read_text())
    assert repro["suite_hash"] == "suite-hash" and repro["seed"] == 3
    assert repro["inputs"]["provenance"] == {"fingerprint": {"vm": "abc"}}
    rows, malformed = read_results(tmp_path / "results.jsonl")
    assert len(rows) == 16 and not malformed
    assert {r["model"] for r in rows} == {"m1", "m2"} and (
        tmp_path / "report" / "report.md"
    ).exists()
    assert rt.scan.roots == [str(tmp_path)]


def test_a_credential_finding_fails_the_job_but_keeps_the_report(tmp_path):
    rt = runtime()
    rt.scan = Scanner([{"file": "./results.jsonl", "offset": 5}])
    outcome = execute_job(make_job(), plan_trials(make_job()), rt, tmp_path)
    assert not outcome.ok and outcome.findings == [{"file": "./results.jsonl", "offset": 5}]
    assert (tmp_path / "report" / "report.md").exists()


def test_the_wall_clock_cap_stops_the_job_with_a_partial_but_valid_result(tmp_path):
    now = {"t": 0.0}

    def execute(trial):
        now["t"] += 10
        return passing(trial)

    job = make_job(wall_clock_cap=35)
    outcome = execute_job(job, plan_trials(job), runtime(execute), tmp_path, clock=lambda: now["t"])
    assert (
        not outcome.ok and outcome.trials_run == 4 and "wall-clock cap" in (outcome.stopped or "")
    )
    rows, malformed = read_results(tmp_path / "results.jsonl")
    assert len(rows) == 4 and not malformed and (tmp_path / "report" / "summary.json").exists()


def test_a_target_that_never_recovers_aborts_the_job_and_still_reports(tmp_path):
    now = {"t": 0.0}
    ran: list[int] = []

    def execute(trial):
        ran.append(trial.index)
        return passing(trial)

    calls = {"n": 0}

    def health():
        calls["n"] += 1
        return (
            [] if calls["n"] <= 2 else ["tunnel 18787: ssh exited"]
        )  # healthy before and after the first trial only

    def tick():  # every look at the clock is 400 s later, so the pause timeout is reached at once
        now["t"] += 400
        return now["t"]

    job = make_job()
    rt = runtime(
        execute,
        health=health,
        runner_options={"clock": tick, "sleep": lambda s: None, "pause_timeout": 60, "poll": 5},
    )
    outcome = execute_job(job, plan_trials(job), rt, tmp_path, clock=tick)
    assert not outcome.ok and "unhealthy" in (outcome.stopped or "")
    assert len(ran) == 1  # only the trial that started while the target was healthy ran
    assert (tmp_path / "report" / "report.md").exists()


def test_gates_skip_broken_tasks_and_unavailable_models(tmp_path):
    job = make_job(tasks=("a", "bad"), models=("up", "down"), arms=(Arm("default"),), trials=1)
    infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "502"}

    def execute(trial):
        return infra if trial.model == "down" else passing(trial)

    gates = Gates(lambda t: (t.task != "bad", "oracle exited 1"), execute, canary_task="a")
    rt = Runtime(
        {("vm", "qwen", "default"): execute},
        Scanner(),
        gates=gates,
        runner_options={"sleep": lambda seconds: None},
        defer_timeout=120,
    )
    outcome = execute_job(job, plan_trials(job), rt, tmp_path)
    rows, _ = read_results(tmp_path / "results.jsonl")
    by = {(r["model"], r["task"]): r["status"] for r in rows}
    assert by[("up", "a")] == "pass" and by[("up", "bad")] == "broken"
    assert by[("down", "a")] == "unavailable" and by[("down", "bad")] == "unavailable"
    text = (tmp_path / "report" / "report.md").read_text()
    assert "## Broken tasks" in text and "## Unavailable cells" in text
    assert outcome.ok and GateFailure("x", "y").status == "x"


def test_a_missing_executor_stops_the_job_cleanly(tmp_path):
    job = make_job()
    rt = Runtime({}, Scanner())
    outcome = execute_job(job, plan_trials(job), rt, tmp_path)
    assert not outcome.ok and outcome.trials_run == 0 and "no executor" in (outcome.stopped or "")
    assert Path(tmp_path / "results.jsonl").parent.exists()


class Timeline:
    """A clock that only moves when the job sleeps or a trial 'takes' time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_model_that_recovers_gets_its_parked_trials_run_later(tmp_path):
    clock = Timeline()
    job = make_job(tasks=("a", "b"), models=("steady", "flaky"), arms=(Arm("default"),), trials=2)
    infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "429"}

    def execute(trial):
        clock.now += 10
        return infra if trial.model == "flaky" and clock.now < 500 else passing(trial)

    gates = Gates(lambda t: (True, ""), execute, cooldown=300, clock=clock)
    rt = Runtime(
        {("vm", "qwen", "default"): execute},
        Scanner(),
        gates=gates,
        runner_options={"clock": clock, "sleep": clock.sleep, "cooldown": 300},
        defer_poll=60,
        defer_timeout=3600,
    )
    outcome = execute_job(job, plan_trials(job), rt, tmp_path, clock=clock)
    rows, _ = read_results(tmp_path / "results.jsonl")
    flaky = [r for r in rows if r["model"] == "flaky"]
    assert len(flaky) == 4 and {r["status"] for r in flaky} == {"pass"}  # every trial ran, once
    assert not any(r["status"] == "unavailable" for r in rows)
    assert outcome.trials_run == 8 and outcome.ok


def test_a_model_that_never_recovers_is_recorded_unavailable_once_per_trial(tmp_path):
    clock = Timeline()
    job = make_job(tasks=("a",), models=("down",), arms=(Arm("default"),), trials=3)
    infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "429"}
    gates = Gates(lambda t: (True, ""), lambda t: infra, cooldown=300, clock=clock)
    rt = Runtime(
        {("vm", "qwen", "default"): lambda t: infra},
        Scanner(),
        gates=gates,
        runner_options={"clock": clock, "sleep": clock.sleep, "cooldown": 300},
        defer_poll=60,
        defer_timeout=1200,
    )
    outcome = execute_job(job, plan_trials(job), rt, tmp_path, clock=clock)
    rows, _ = read_results(tmp_path / "results.jsonl")
    assert [r["status"] for r in rows] == ["unavailable"] * 3  # written once each, at the end
    assert outcome.trials_run == 0 and clock.now >= 1200  # waited its bounded time, no more
