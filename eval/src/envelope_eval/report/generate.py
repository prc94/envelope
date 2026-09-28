from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..runner.results import read_results
from .aggregate import aggregate, render_markdown
from .compare import evaluate_baselines, evaluate_rules


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def generate(
    results: str | Path,
    output: str | Path,
    *,
    profile: str = "screening",
    rules: Iterable[dict[str, Any]] = (),
    secrets: Iterable[str] = (),
    samples: int = 2000,
    seed: int = 0,
    baselines: Iterable[dict[str, Any]] = (),
    task_tags: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Write `report.md` and `summary.json` for a results file (complete or interrupted).

    Trial records may carry `trial_secrets` (gateway tokens, database passwords); those and
    any extra `secrets` are removed from everything written and dropped from the summary.
    """
    rows, malformed = read_results(results)
    known = list(secrets)
    for row in rows:
        known.extend(row.pop("trial_secrets", None) or [])
    summary = aggregate(rows)
    summary["malformed"] = malformed
    summary["profile"] = profile
    summary["decisions"] = evaluate_rules(rows, list(rules), profile, samples, seed)
    summary["baselines"] = evaluate_baselines(rows, list(baselines), task_tags, samples, seed)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    text = redact(json.dumps(summary, indent=2), known)
    (out / "summary.json").write_text(text, encoding="utf-8")
    (out / "report.md").write_text(redact(render_markdown(summary), known), encoding="utf-8")
    return json.loads(text)
