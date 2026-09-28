"""The M1 job file, task selection and the command line."""

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from envelope_eval.cli import main
from envelope_eval.runner.planner import load_job, plan_trials
from envelope_eval.runner.wiring import JobConfigError, expand_tasks
from envelope_eval.tasks import load_suite

ROOT = Path(__file__).parents[2]
JOB = ROOT / "eval" / "jobs" / "m1-decision.toml"
SUITE = load_suite(ROOT / "eval" / "tasks")


def test_the_m1_job_declares_arms_settings_variant_rules_and_the_target():
    job = load_job(JOB)
    assert job.profile == "decision" and job.trials >= 5 and job.wall_clock_cap == 43200
    assert job.models == (
        "deepseek/deepseek-v4-flash-0731",
        "qwen/qwen3.8-flash",
        "z-ai/glm-5.3-flash",
    )
    arms = {a.name: a for a in job.arms}
    assert set(arms) == {"default", "budget-small", "budget-large", "headtail-half"}
    assert arms["budget-small"].settings == {
        "ENVELOPE_OUTPUT_MAX_CHARS": "6000",
        "ENVELOPE_OUTPUT_MAX_LINES": "200",
    }
    assert arms["headtail-half"].variant == "head-half" and arms["default"].settings == {}
    extra: Any = job.extra  # free-form tables
    assert extra["variant"]["head-half"]["patch"] == "eval/variants/head-half.patch"
    assert (ROOT / "eval/variants/head-half.patch").exists()
    assert extra["target"]["vm"]["fingerprint"]["oemcp"] == 866
    assert {r["name"] for r in job.decision_rules} == {
        "adopt-budget-small",
        "adopt-budget-large",
        "adopt-headtail-half",
    }
    assert all(len(r["conditions"]) == 2 for r in job.decision_rules)  # type: ignore[arg-type]


def test_tag_selectors_expand_to_tasks_and_unknown_ones_are_errors():
    job = load_job(JOB)
    ids = expand_tasks(job.tasks, SUITE, job.lanes)
    assert "logs-trace-id" in ids and "canary" not in ids and "gitbash-cp866-dir" not in ids
    assert expand_tasks(("canary", "tag:canary"), SUITE, ("linux",)) == ("canary",)
    with pytest.raises(JobConfigError, match="unknown task"):
        expand_tasks(("no-such-task",), SUITE, ("windows",))
    with pytest.raises(JobConfigError, match="no task has the tag"):
        expand_tasks(("tag:no-such-tag",), SUITE, ("windows",))
    assert expand_tasks(("gitbash-cp866-dir",), SUITE, ("linux",)) == ()  # Windows-only task


def test_arms_run_only_on_tasks_carrying_their_lever_tag():
    job = load_job(JOB)
    job = dataclasses.replace(job, tasks=expand_tasks(job.tasks, SUITE, job.lanes))
    tags = {t.id: set(t.tags) for t in SUITE.tasks}
    plan = plan_trials(job, tags)
    head_tail_tasks = {t.id for t in SUITE.tasks if "head-tail" in t.tags}
    assert {t.task for t in plan if t.arm == "headtail-half"} <= head_tail_tasks
    assert {t.task for t in plan if t.arm == "budget-small"} == set(job.tasks)
    per_cell = {}
    for t in plan:
        per_cell[(t.task, t.arm, t.model)] = per_cell.get((t.task, t.arm, t.model), 0) + 1
    assert set(per_cell.values()) == {job.trials}  # every cell gets the declared trials
    assert plan == plan_trials(job, tags)  # reproducible from the seed


def test_the_plan_command_reports_size_and_time(capsys):
    assert main(["plan", str(JOB)]) == 0
    text = capsys.readouterr().out
    assert "trials over" in text and "wall-clock cap 43200" in text and "logs-trace-id" in text


def test_a_job_refuses_to_start_near_the_end_of_the_evaluation_licence():
    from datetime import date

    from envelope_eval.runner.wiring import check_licence

    cfg = {"expires": date(2026, 12, 23)}
    check_licence("vm", cfg, today=date(2026, 12, 19))  # four days left: fine
    check_licence("vm", {}, today=date(2030, 1, 1))  # no declared expiry: nothing to check
    for today in (date(2026, 12, 20), date(2026, 12, 23), date(2027, 1, 5)):
        with pytest.raises(JobConfigError, match="evaluation licence ends on 2026-12-23"):
            check_licence("vm", cfg, today=today)
    check_licence("vm", {"expires": "2026-12-23"}, today=date(2026, 9, 26))  # a string works too
    extra: Any = load_job(JOB).extra
    assert str(extra["target"]["vm"]["expires"]) == "2026-12-23"
