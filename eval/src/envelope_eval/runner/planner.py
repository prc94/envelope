"""Job loading and deterministic interleaved trial planning."""

from __future__ import annotations

import random
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Arm:
    name: str
    kind: str = "envelope"
    tags: frozenset[str] = frozenset()
    variant: str | None = None
    tool_set: tuple[str, ...] = ()
    settings: dict[str, str] = field(default_factory=dict)  # ENVELOPE_* for this arm's envelope


@dataclass(frozen=True)
class Job:
    name: str
    seed: int
    lanes: tuple[str, ...]
    tasks: tuple[str, ...]
    arms: tuple[Arm, ...]
    trials: int = 1
    profile: str = "screening"
    wall_clock_cap: float | None = None
    targets: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    decision_rules: tuple[dict[str, object], ...] = ()
    target_support: dict[str, tuple[str, ...]] = field(default_factory=dict)
    harness_support: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Tables the driver reads: `target.<name>`, `harness.<name>`, `variant.<name>`, `gateway`.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Trial:
    index: int
    lane: str
    task: str
    arm: str
    seed: int
    target: str = ""
    harness: str = ""
    model: str = ""


def load_job(path: str | Path) -> Job:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    arms = tuple(
        Arm(
            a["name"],
            a.get("kind", "envelope"),
            frozenset(a.get("tags", [])),
            a.get("variant"),
            tuple(a.get("tool_set", [])),
            {str(k): str(v) for k, v in a.get("settings", {}).items()},
        )
        for a in data.get("arms", [])
    )
    support = data.get("support", {})
    return Job(
        name=Path(path).stem,
        seed=int(data.get("seed", 0)),
        lanes=tuple(data.get("lanes", [])),
        tasks=tuple(data.get("tasks", [])),
        arms=arms,
        trials=int(data.get("trials", 1)),
        profile=data.get("profile", "screening"),
        wall_clock_cap=data.get("wall_clock_cap"),
        targets=tuple(data.get("targets", [])),
        harnesses=tuple(data.get("harnesses", [])),
        models=tuple(data.get("models", [])),
        decision_rules=tuple(data.get("decision_rules", [])),
        target_support={k: tuple(v) for k, v in support.get("targets", {}).items()},
        harness_support={k: tuple(v) for k, v in support.get("harnesses", {}).items()},
        extra={
            k: data[k] for k in ("target", "harness", "variant", "gateway", "baseline") if k in data
        },
    )


def plan_trials(
    job: Job,
    task_tags: dict[str, set[str]] | None = None,
    baseline_eligible: dict[str, bool] | None = None,
) -> tuple[Trial, ...]:
    task_tags = task_tags or {}
    baseline_eligible = baseline_eligible or {}
    rng = random.Random(job.seed)
    targets = job.targets or ("",)
    harnesses = job.harnesses or ("",)
    models = job.models or ("",)
    pending: list[tuple[str, str, str, str, str, str]] = []
    for lane in job.lanes:
        for target in targets:
            if job.target_support.get(target) and lane not in job.target_support[target]:
                continue
            for harness in harnesses:
                if job.harness_support.get(harness) and target not in job.harness_support[harness]:
                    continue
                for model in models:
                    for task in job.tasks:
                        for arm in job.arms:
                            if arm.tags and not (arm.tags & task_tags.get(task, set())):
                                continue
                            if arm.kind == "native" and not baseline_eligible.get(task, False):
                                continue
                            for _ in range(job.trials):
                                pending.append((lane, task, arm.name, target, harness, model))
    rng.shuffle(pending)
    return tuple(
        Trial(i, lane, task, arm, job.seed + i, target, harness, model)
        for i, (lane, task, arm, target, harness, model) in enumerate(pending)
    )
