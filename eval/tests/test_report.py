import json
from pathlib import Path

from envelope_eval.report.aggregate import aggregate, render_markdown
from envelope_eval.report.compare import (
    evaluate_baselines,
    evaluate_rule,
    evaluate_rules,
    paired_difference,
    paired_success,
)
from envelope_eval.report.generate import generate
from envelope_eval.runner.results import ResultWriter


def rows():
    return [
        {
            "lane": "linux",
            "task": "a",
            "harness": "qwen",
            "arm": "default",
            "passed": True,
            "tokens": 10,
            "turns": 2,
            "tool_sequence": ["run"],
        },
        {
            "lane": "linux",
            "task": "a",
            "harness": "qwen",
            "arm": "budget",
            "passed": True,
            "tokens": 8,
            "turns": 2,
            "tool_sequence": ["run"],
        },
        {
            "lane": "linux",
            "task": "b",
            "harness": "qwen",
            "arm": "default",
            "passed": False,
            "attribution": "tool",
            "tokens": 12,
        },
        {
            "lane": "linux",
            "task": "b",
            "harness": "qwen",
            "arm": "budget",
            "passed": True,
            "tokens": 9,
        },
    ]


def test_aggregate_and_pair_rule():
    summary = aggregate(rows())
    cell = summary["cells"]["linux|a|qwen||default"]  # lane|task|harness|model|arm
    assert cell["count"] == 1
    comparison = paired_success(rows(), "default", "budget", samples=50)
    assert comparison["estimate"] == 0.5
    assert evaluate_rule(comparison, minimum_improvement=0.1) in {"met", "inconclusive"}


def test_partial_report_lists_malformed(tmp_path: Path):
    source = tmp_path / "results.jsonl"
    writer = ResultWriter(source)
    writer.append(rows()[0])
    source.write_text(source.read_text() + "{bad\n")
    summary = generate(source, tmp_path / "report")
    assert summary["malformed"] and (tmp_path / "report" / "report.md").exists()
    assert json.loads((tmp_path / "report" / "summary.json").read_text())["malformed"]


def _trials(arm, task, passes, tokens, **extra):
    return [
        {
            "lane": "windows",
            "task": task,
            "harness": "qwen",
            "arm": arm,
            "passed": ok,
            "tokens": tokens,
            "turns": 4,
            **extra,
        }
        for ok in passes
    ]


def _synthetic(arm_passes, arm_tokens, tasks=8):
    """`default` always passes with 1000 tokens; `arm` gets the given passes/tokens per task."""
    out = []
    for n in range(tasks):
        out += _trials("default", f"t{n}", [True] * 4, 1000)
        out += _trials("arm", f"t{n}", arm_passes[n % len(arm_passes)], arm_tokens)
    return out


RULE = {
    "name": "adopt-budget",
    "arm": "arm",
    "versus": "default",
    "conditions": [
        {"metric": "success", "kind": "noninferiority", "margin": 0.05},
        {"metric": "tokens", "kind": "improvement", "margin": 0.15},
    ],
}


def _verdict(records, profile="decision"):
    return evaluate_rules(records, [RULE], profile, samples=400)["rules"][0]


def test_rule_met_when_success_holds_and_tokens_fall_enough():
    rule = _verdict(_synthetic([[True] * 4], 700))
    assert rule["verdict"] == "met"
    success, tokens = rule["conditions"]
    assert success["estimate"] == 0 and success["tasks"] == 8
    assert abs(tokens["estimate"] - 0.3) < 1e-9 and tokens["width"] == 0


def test_rule_not_met_when_success_drops_well_past_the_margin():
    rule = _verdict(_synthetic([[True, True, False, False]], 700))
    assert rule["verdict"] == "not met"
    assert [c["verdict"] for c in rule["conditions"]] == ["not met", "met"]
    assert rule["conditions"][0]["estimate"] == -0.5


def test_rule_not_met_when_tokens_do_not_fall_enough():
    rule = _verdict(_synthetic([[True] * 4], 950))
    assert rule["verdict"] == "not met"
    assert [c["verdict"] for c in rule["conditions"]] == ["met", "not met"]


def test_rule_inconclusive_when_the_interval_straddles_the_margin():
    one_bad_task = [[False] * 4] + [[True] * 4] * 7  # mean -0.125, interval [-0.4, 0]
    assert _verdict(_synthetic(one_bad_task, 700))["conditions"][0]["verdict"] == "inconclusive"
    assert _verdict(_synthetic(one_bad_task, 700))["verdict"] == "inconclusive"
    assert _verdict([])["verdict"] == "inconclusive"  # no paired data at all


def test_screening_jobs_are_exploratory_and_get_no_verdicts():
    result = evaluate_rules(_synthetic([[True] * 4], 700), [RULE], "screening", samples=50)
    assert result["exploratory"] is True
    assert result["rules"][0]["verdict"] is None
    assert all("verdict" not in c for c in result["rules"][0]["conditions"])


def test_infrastructure_and_broken_trials_do_not_enter_comparisons():
    records = _synthetic([[True] * 4], 700)
    records += _trials("arm", "t0", [False] * 4, 700, attribution="infra")
    records += _trials("arm", "t1", [False] * 4, 700, status="broken")
    assert _verdict(records)["verdict"] == "met"


def test_recovery_metric_uses_only_truncated_trials():
    records = []
    for n in range(6):
        records += _trials(
            "default", f"t{n}", [True], 900, truncated=True, truncation_recovery=False
        )
        records += _trials("arm", f"t{n}", [True], 900, truncated=True, truncation_recovery=True)
        records += _trials("arm", f"t{n}", [True], 900)  # not truncated: ignored
    comparison = paired_difference(records, "arm", "default", "recovery", samples=100)
    assert comparison["estimate"] == 1.0 and comparison["tasks"] == 6


def test_report_shows_rules_labels_exploratory_and_redacts_secrets(tmp_path: Path):
    source = tmp_path / "results.jsonl"
    writer = ResultWriter(source)
    for row in _synthetic([[True] * 4], 700):
        writer.append(row)
    writer.append(
        {
            **_trials("arm", "t0", [True], 700)[0],
            "known_gaps": ["gap-1"],
            "trial_secrets": ["hunter2-PASSWORD"],
        }
    )
    writer.append(  # a broken-task reason can quote a command line that carried a password
        {
            "lane": "windows",
            "task": "t9",
            "harness": "qwen",
            "arm": "arm",
            "status": "broken",
            "reason": "oracle: redis-cli -a hunter2-PASSWORD failed",
            "trial_secrets": ["hunter2-PASSWORD"],
        }
    )
    decision = generate(source, tmp_path / "d", profile="decision", rules=[RULE], samples=50)
    assert decision["decisions"]["rules"][0]["verdict"] == "met"
    text = (tmp_path / "d" / "report.md").read_text() + (
        tmp_path / "d" / "summary.json"
    ).read_text()
    assert "adopt-budget: met" in text and "hunter2-PASSWORD" not in text
    assert "[redacted]" in text and "Exploratory" not in text
    generate(source, tmp_path / "s", profile="screening", rules=[RULE], samples=50)
    assert "Exploratory" in (tmp_path / "s" / "report.md").read_text()


def test_cells_are_per_model_and_known_gaps_are_listed_apart_but_counted():
    rows_ = []
    for model, passes in (("m1", [True, True]), ("m2", [True, False])):
        rows_ += _trials("default", "t", passes, 100, model=model)
    rows_ += _trials(
        "default", "gap-task", [False, False, True], 100, known_gaps=["gap-x"], attribution="model"
    )[:3]
    rows_[-3]["attribution"] = "model"
    summary = aggregate(rows_)
    assert {c["model"] for c in summary["cells"].values()} == {"m1", "m2", ""}
    assert summary["cells"]["windows|t|qwen|m1|default"]["successes"] == 2
    assert summary["cells"]["windows|t|qwen|m2|default"]["successes"] == 1  # not pooled
    gap = summary["known_gaps"]["gap-x"]
    assert gap["count"] == 3 and gap["successes"] == 1 and gap["attributed_failures"]["model"] >= 1
    text = render_markdown(summary)
    main_table = text.split("## Known gaps")[0]
    assert "gap-task" not in main_table and "|t|qwen|m1|" in main_table
    assert "### `gap-x`: 1/3 passed" in text and "gap-task" in text.split("## Known gaps")[1]


def test_broken_and_unavailable_trials_get_their_own_sections():
    records = [
        {
            "lane": "l",
            "task": "a",
            "harness": "h",
            "model": "m",
            "arm": "x",
            "status": "broken",
            "reason": "oracle failed: exit 1",
        },
        {
            "lane": "l",
            "task": "b",
            "harness": "h",
            "model": "m",
            "arm": "x",
            "status": "unavailable",
            "reason": "circuit breaker tripped",
            "failures": ["502", "502"],
        },
        {
            "lane": "l",
            "task": "c",
            "harness": "h",
            "model": "m",
            "arm": "x",
            "gate": "canary",
            "passed": False,
        },
    ]
    summary = aggregate(records)
    assert not any("|c|" in name for name in summary["cells"])  # gate probes are not results
    text = render_markdown(summary)
    assert "## Broken tasks" in text and "oracle failed: exit 1" in text
    assert "## Unavailable cells" in text and "502; 502" in text


def test_comparisons_stratify_by_model_and_can_be_restricted_to_one():
    records = []
    for model, arm_tokens in (("m1", 500), ("m2", 1500)):  # the arm helps m1 and hurts m2
        for n in range(4):
            records += _trials("default", f"t{n}", [True] * 2, 1000, model=model)
            records += _trials("arm", f"t{n}", [True] * 2, arm_tokens, model=model)
    both = paired_difference(records, "arm", "default", "tokens", samples=200)
    only1 = paired_difference(records, "arm", "default", "tokens", samples=200, model="m1")
    only2 = paired_difference(records, "arm", "default", "tokens", samples=200, model="m2")
    assert both["tasks"] == 8 and only1["tasks"] == 4  # (model, task) strata
    assert only1["estimate"] == 0.5 and only2["estimate"] == -0.5 and both["estimate"] == 0
    rule = {"arm": "arm", "versus": "default", "model": "m1",
            "conditions": [{"metric": "tokens", "kind": "improvement", "margin": 0.3}]}  # fmt: skip
    verdict = evaluate_rules(records, [rule], "decision", samples=200)["rules"][0]
    assert verdict["verdict"] == "met" and verdict["conditions"][0]["model"] == "m1"


def _baseline_records():
    out = []
    for n in range(6):
        # envelope: fewer tokens and turns; native: fails two tasks, one of them by a hanging tool
        out += _trials("default", f"t{n}", [True] * 3, 800, turns=4, model="m")
        native_ok = n not in (0, 1)
        extra = {"attribution": "tool"} if n == 0 else {"attribution": "model"} if n == 1 else {}
        out += _trials("native", f"t{n}", [native_ok] * 3, 1000, turns=6, model="m", **extra)
    return out


def test_baseline_section_compares_native_to_envelope_on_shared_tasks():
    entries = evaluate_baselines(
        _baseline_records(),
        [{"arm": "native", "versus": "default", "name": "windows-goal"}],
        samples=200,
    )
    (entry,) = entries
    assert entry["claim"] == "goal" and entry["arm"] == "native" and entry["versus"] == "default"
    assert entry["success"]["tasks"] == 6 and abs(entry["success"]["estimate"] - 1 / 3) < 1e-9
    assert abs(entry["tokens"]["estimate"] - 0.2) < 1e-9  # 800 against 1000: 20% fewer
    assert abs(entry["turns"]["estimate"] - 1 / 3) < 1e-9  # 4 against 6
    # failures of the native arm: task 0 is a `tool` failure (3 trials), task 1 a model failure
    assert entry["failures"]["native"] == {"tool": 3, "harness": 0}
    assert entry["failures"]["default"] == {"tool": 0, "harness": 0}


def test_the_your_shell_claim_uses_its_own_tasks():
    records = _baseline_records()
    tags = {f"t{n}": ({"your-shell"} if n >= 4 else set()) for n in range(6)}
    goal, shell = evaluate_baselines(
        records,
        [
            {"arm": "native", "versus": "default", "name": "goal"},
            {"arm": "native", "versus": "default", "name": "shell", "tag": "your-shell",
             "claim": "your-shell"},
        ],
        tags,
        samples=100,
    )  # fmt: skip
    assert goal["success"]["tasks"] == 6 and shell["success"]["tasks"] == 2
    assert shell["success"]["estimate"] == 0  # both pass t4 and t5
    assert shell["claim"] == "your-shell" and shell["tag"] == "your-shell"


def test_the_baseline_section_is_rendered_and_nothing_appears_without_a_baseline(tmp_path):
    source = tmp_path / "r.jsonl"
    writer = ResultWriter(source)
    for row in _baseline_records():
        writer.append(row)
    generate(
        source, tmp_path / "with", baselines=[{"arm": "native", "versus": "default"}], samples=50
    )
    text = (tmp_path / "with" / "report.md").read_text()
    assert "## Baseline: native shell tool versus envelope" in text
    assert "failures of `native` attributed to the tool: 3" in text
    assert "tokens, relative reduction: +0.200" in text
    generate(source, tmp_path / "without", samples=50)
    assert "Baseline" not in (tmp_path / "without" / "report.md").read_text()


def test_the_report_counts_trials_with_malformed_tool_calls_per_cell():
    rows_ = _trials("default", "t", [False, False, True], 100, model="m")
    rows_[0]["timeline"] = [{"kind": "malformed_call"}]
    rows_[1]["timeline"] = [{"kind": "request_start"}]
    summary = aggregate(rows_)
    (cell,) = summary["cells"].values()
    assert cell["malformed_calls"] == 1
    assert "| 1 |" in render_markdown(summary) and "Malformed calls" in render_markdown(summary)


def test_providers_are_listed_per_cell_and_a_mixed_model_is_flagged():
    rows_ = _trials("default", "t", [True, True], 100, model="m1")
    rows_[0]["providers"], rows_[1]["providers"] = ["A"], ["A", "B"]
    rows_ += _trials("default", "t", [True], 100, model="m2")
    rows_[-1]["providers"] = ["C"]
    summary = aggregate(rows_)
    assert summary["cells"]["windows|t|qwen|m1|default"]["providers"] == ["A", "B"]
    text = render_markdown(summary)
    assert "| A, B |" in text and "m1 was served by A, B" in text and "m2 was served" not in text
