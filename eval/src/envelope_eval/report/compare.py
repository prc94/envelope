"""Task-stratified paired arm comparisons and decision rules.

A comparison is paired by task: for every task that has trials in both arms, the arm's
per-task statistic is compared with the reference arm's, and the mean of those per-task
differences is the estimate. Its 95% interval comes from resampling tasks with replacement.
Differences are always oriented so that positive means the arm is better than the reference:
success and recovery are absolute differences in rate, tokens and turns are relative
reductions of the per-task median.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

Record = dict[str, Any]
METRICS = ("success", "recovery", "tokens", "turns")
# Trials that did not measure the arm (infrastructure failures, broken or unavailable cells)
# are left out of comparisons; they are reported in their own report sections.
EXCLUDED_STATUS = {"broken", "unavailable"}


def _passed(row: Record) -> bool:
    return bool(row.get("passed", row.get("verdict") == "pass"))


def _usable(row: Record) -> bool:
    return row.get("status") not in EXCLUDED_STATUS and row.get("attribution") != "infra"


def _rate(rows: list[Record]) -> float | None:
    return sum(_passed(row) for row in rows) / len(rows) if rows else None


def _recovery(rows: list[Record]) -> float | None:
    truncated = [row for row in rows if row.get("truncated")]
    if not truncated:
        return None
    return sum(bool(row.get("truncation_recovery")) for row in truncated) / len(truncated)


def _median_of(key: str) -> Callable[[list[Record]], float | None]:
    def statistic(rows: list[Record]) -> float | None:
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
        return statistics.median(values) if values else None

    return statistic


_STATISTIC: dict[str, Callable[[list[Record]], float | None]] = {
    "success": _rate,
    "recovery": _recovery,
    "tokens": _median_of("tokens"),
    "turns": _median_of("turns"),
}
_RELATIVE = {"tokens", "turns"}


def _bootstrap(diffs: list[float], samples: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(samples))
    return means[int(samples * 0.025)], means[min(samples - 1, int(samples * 0.975))]


def paired_difference(
    records: list[Record],
    arm: str,
    versus: str,
    metric: str = "success",
    samples: int = 2000,
    seed: int = 0,
    model: str | None = None,
) -> dict[str, Any]:
    """Paired, task-stratified difference of `arm` relative to `versus` (positive = better).
    Strata are (model, task) pairs; `model` restricts the comparison to one model."""
    if metric not in _STATISTIC:
        raise ValueError(f"unknown metric {metric!r}; expected one of {', '.join(METRICS)}")
    statistic = _STATISTIC[metric]
    by_task: dict[tuple[str, str], dict[str, list[Record]]] = defaultdict(lambda: defaultdict(list))
    for row in records:
        if row.get("arm") in (arm, versus) and _usable(row) and model in (None, row.get("model")):
            by_task[(row.get("model", ""), row.get("task", ""))][row["arm"]].append(row)
    diffs: list[float] = []
    for task in sorted(by_task):  # a stratum is one task on one model: models are never pooled
        mine, reference = statistic(by_task[task][arm]), statistic(by_task[task][versus])
        if mine is None or reference is None:
            continue
        if metric in _RELATIVE:
            if reference == 0:
                continue
            diffs.append((reference - mine) / reference)
        else:
            diffs.append(mine - reference)
    result: dict[str, Any] = {
        "arm": arm,
        "versus": versus,
        "metric": metric,
        "model": model,
        "tasks": len(diffs),
        "estimate": None,
        "interval": None,
        "width": None,
    }
    if diffs:
        lo, hi = _bootstrap(diffs, samples, seed)
        result.update(estimate=sum(diffs) / len(diffs), interval=(lo, hi), width=hi - lo)
    return result


def paired_success(
    records: list[Record], left: str, right: str, samples: int = 2000, seed: int = 0
) -> dict[str, Any]:
    """Success-rate difference `right - left` (kept for callers that name arms by side)."""
    return paired_difference(records, right, left, "success", samples, seed)


def evaluate_rule(
    comparison: dict[str, Any],
    *,
    noninferiority_margin: float | None = None,
    minimum_improvement: float | None = None,
) -> str:
    """Judge one comparison against a margin: "met", "not met" or "inconclusive".

    Non-inferiority (the arm is at most `margin` worse): met when the interval's lower bound
    is above -margin, not met when its upper bound is below it. Minimum improvement: met when
    the lower bound reaches the margin, not met when the upper bound falls short of it.
    Anything in between, or no data, is inconclusive.
    """
    if (noninferiority_margin is None) == (minimum_improvement is None):
        raise ValueError("give exactly one of noninferiority_margin or minimum_improvement")
    interval = comparison.get("interval")
    if interval is None:
        return "inconclusive"
    lo, hi = interval
    if noninferiority_margin is not None:
        floor = -noninferiority_margin
        if lo > floor:
            return "met"
        return "not met" if hi < floor else "inconclusive"
    assert minimum_improvement is not None
    if lo >= minimum_improvement:
        return "met"
    return "not met" if hi < minimum_improvement else "inconclusive"


def _combine(verdicts: list[str]) -> str:
    if "not met" in verdicts:
        return "not met"
    if not verdicts or "inconclusive" in verdicts:
        return "inconclusive"
    return "met"


def evaluate_rules(
    records: list[Record],
    rules: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    profile: str,
    samples: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    """Evaluate a job's declared decision rules against its results.

    A rule names the `arm`, the reference arm (`versus`) and one or more conditions, each a
    `metric` with a `kind` (`noninferiority` or `improvement`) and a `margin` (and optionally
    the `model` it applies to; without one, strata of every model are compared side by side); a rule with a
    single condition may put them at its top level. All conditions must be met for the rule
    to be met; one condition not met is enough for the rule to be not met. Screening jobs are
    exploratory: comparisons are reported but no verdicts are given.
    """
    exploratory = profile == "screening"
    evaluated = []
    for index, rule in enumerate(rules):
        conditions = rule.get("conditions") or [rule]
        parts = []
        for condition in conditions:
            comparison = paired_difference(
                records,
                rule["arm"],
                rule["versus"],
                condition["metric"],
                samples,
                seed,
                rule.get("model"),
            )
            kind = condition["kind"]
            if kind not in ("noninferiority", "improvement"):
                raise ValueError(f"unknown rule kind {kind!r}")
            part = comparison | {"kind": kind, "margin": condition["margin"]}
            if not exploratory:
                if kind == "noninferiority":
                    part["verdict"] = evaluate_rule(
                        comparison, noninferiority_margin=condition["margin"]
                    )
                else:
                    part["verdict"] = evaluate_rule(
                        comparison, minimum_improvement=condition["margin"]
                    )
            parts.append(part)
        evaluated.append(
            {
                "name": rule.get("name", f"rule-{index + 1}"),
                "arm": rule["arm"],
                "versus": rule["versus"],
                "conditions": parts,
                "verdict": None if exploratory else _combine([p["verdict"] for p in parts]),
            }
        )
    return {"exploratory": exploratory, "rules": evaluated}


def evaluate_baselines(
    records: list[Record],
    baselines: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    task_tags: dict[str, set[str]] | None = None,
    samples: int = 2000,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Native-shell baseline against an envelope arm, on the tasks both ran.

    Each baseline names `arm` (the native arm), `versus` (the envelope arm), optionally `model`
    and a `tag`: only tasks carrying it are compared. That is how a separate claim gets its own
    tasks (on Windows the goal-stated tasks answer "does envelope beat the native tool", while
    tasks tagged `your-shell` answer "does the agent work in the user's own shell"). Every difference
    is oriented so that positive favours the envelope arm; failures attributed to the tool or the
    harness are counted per arm.
    """
    task_tags = task_tags or {}
    out = []
    for index, spec in enumerate(baselines):
        tag = spec.get("tag")
        rows = [r for r in records if tag is None or tag in task_tags.get(r.get("task", ""), set())]
        native, envelope = spec["arm"], spec["versus"]
        entry: dict[str, Any] = {
            "name": spec.get("name", f"baseline-{index + 1}"),
            "claim": spec.get("claim", tag or "goal"),
            "arm": native,
            "versus": envelope,
            "tag": tag,
            "model": spec.get("model"),
        }
        for metric in ("success", "turns", "tokens"):
            entry[metric] = paired_difference(
                rows, envelope, native, metric, samples, seed, spec.get("model")
            )
        entry["failures"] = {
            arm: {
                kind: sum(
                    1
                    for r in rows
                    if r.get("arm") == arm
                    and not _passed(r)
                    and r.get("attribution") == kind
                    and spec.get("model") in (None, r.get("model"))
                )
                for kind in ("tool", "harness")
            }
            for arm in (native, envelope)
        }
        out.append(entry)
    return out
