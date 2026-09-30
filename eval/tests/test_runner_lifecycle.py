import json
from pathlib import Path

from envelope_eval.runner.lifecycle import finish_job, reproducibility
from envelope_eval.runner.planner import Arm, Job, plan_trials


def test_reproducibility_and_interleaved_order(tmp_path: Path):
    job = Job("job", 7, ("linux",), ("a", "b"), (Arm("default"),), trials=2)
    order = plan_trials(job)
    record = reproducibility(job, order, "suite")
    path = tmp_path / "repro.json"
    record.write(path)
    assert json.loads(path.read_text())["seed"] == 7
    assert plan_trials(job) == order


def test_planner_filters_unsupported_target_and_harness():
    from envelope_eval.runner.planner import Job

    job = Job(
        "job",
        1,
        ("linux",),
        ("a",),
        (Arm("default"),),
        targets=("vm", "bad"),
        harnesses=("qwen",),
        models=("m",),
        target_support={"vm": ("linux",), "bad": ("windows",)},
        harness_support={"qwen": ("vm",)},
    )
    trials = plan_trials(job)
    assert (
        len(trials) == 1
        and trials[0].target == "vm"
        and trials[0].harness == "qwen"
        and trials[0].model == "m"
    )


def test_planner_restricts_an_arm_to_the_harnesses_it_supports():
    from envelope_eval.runner.planner import Job

    job = Job(
        "job",
        1,
        ("linux",),
        ("a",),
        (Arm("native-qwen", kind="native"), Arm("native-opencode", kind="native")),
        targets=("t",),
        harnesses=("qwen", "opencode"),
        models=("m",),
        arm_support={"native-qwen": ("qwen",), "native-opencode": ("opencode",)},
    )
    trials = plan_trials(job, baseline_eligible={"a": True})
    by_harness = {t.harness: t.arm for t in trials}
    assert len(trials) == 2
    assert by_harness == {"qwen": "native-qwen", "opencode": "native-opencode"}


def test_finish_job_fails_on_any_finding_and_reports_only_locations(tmp_path: Path):
    class Scanner:
        def scan(self, root):
            return [{"file": "./result.jsonl", "offset": 7}]

    result = finish_job(tmp_path, Scanner())
    assert not result["ok"] and result["findings"] == [{"file": "./result.jsonl", "offset": 7}]

    class Clean:
        def scan(self, root):
            return []

    assert finish_job(tmp_path, Clean())["ok"]
