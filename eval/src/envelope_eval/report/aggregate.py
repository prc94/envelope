"""Descriptive aggregation and paired comparisons for evaluation results."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def wilson_interval(
    successes: int, total: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    den = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / den
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / den
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _median(values: Iterable[Any]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return statistics.median(nums) if nums else None


def _cell_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(bool(row.get("passed", row.get("verdict") == "pass")) for row in rows)
    total = len(rows)
    failures: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.get("attribution"):
            failures[row["attribution"]] += 1
    return {
        "count": total,
        "successes": passed,
        "success_rate": passed / total if total else 0.0,
        "wilson_95": wilson_interval(passed, total),
        "attributed_failures": dict(failures),
        "providers": sorted({p for row in rows for p in row.get("providers", [])}),
        "malformed_calls": sum(
            any(e.get("kind") == "malformed_call" for e in row.get("timeline", [])) for row in rows
        ),
    }


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("lane", "task", "harness", "model", "arm", "target", "status", "reason", "failures")
    return {k: record[k] for k in keys if k in record}


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cell statistics (a cell is lane x task x harness x model x arm), known-gap results
    apart from the rest but still counted, and broken or unavailable trials in their own lists."""
    cells: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("gate"):
            continue  # canary gate probes are recorded for audit, not part of the results
        key = (
            record.get("lane", ""),
            record.get("task", ""),
            record.get("harness", ""),
            record.get("model", ""),
            record.get("arm", ""),
        )
        cells[key].append(record)
    out = {}
    gaps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    gap_cells: dict[str, set[str]] = defaultdict(set)
    for key, rows in cells.items():
        sequences = {
            tuple(row.get("tool_sequence", ()))
            for row in rows
            if row.get("tool_sequence") is not None
        }
        name = "|".join(key)
        cell_gaps = sorted({g for row in rows for g in row.get("known_gaps", [])})
        out[name] = {
            "lane": key[0],
            "task": key[1],
            "harness": key[2],
            "model": key[3],
            "arm": key[4],
            **_cell_stats(rows),
            "median_turns": _median(row.get("turns") for row in rows),
            "median_tokens": _median(row.get("tokens") for row in rows),
            "cost": sum(float(row.get("cost", 0) or 0) for row in rows),
            "distinct_sequences": len(sequences),
            "known_gaps": cell_gaps,
        }
        for gap in cell_gaps:
            gaps[gap] += rows
            gap_cells[gap].add(name)
    return {
        "cells": out,
        "known_gaps": {
            gap: {**_cell_stats(gaps[gap]), "cells": sorted(gap_cells[gap])} for gap in sorted(gaps)
        },
        "broken": [_brief(r) for r in records if r.get("status") == "broken"],
        "unavailable": [_brief(r) for r in records if r.get("status") == "unavailable"],
    }


def _pct(cell: dict[str, Any]) -> str:
    lo, hi = cell["wilson_95"]
    return f"{cell['success_rate']:.1%} ({lo:.1%}–{hi:.1%})"


def render_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Evaluation report", ""]
    decisions = summary.get("decisions", {})
    if decisions.get("exploratory"):
        lines += ["> **Exploratory.** Screening profile: no decision rules are evaluated.", ""]
    cells = summary.get("cells", {})
    main = {n: c for n, c in cells.items() if not c.get("known_gaps")}
    lines += [
        "| Cell (lane, task, harness, model, arm) | Trials | Success (95% Wilson) | Median tokens | Malformed calls | Providers |",
        "|---|---:|---|---:|---:|---|",
    ]
    for name, cell in main.items():
        lines.append(
            f"| {name} | {cell['count']} | {_pct(cell)} | {cell['median_tokens'] or '—'} | {cell['malformed_calls']} | {', '.join(cell['providers']) or '—'} |"
        )
    lines += _render_provider_split(summary.get("cells", {}))
    lines += _render_gaps(summary.get("known_gaps", {}), cells)
    for title, key in (("Broken tasks", "broken"), ("Unavailable cells", "unavailable")):
        rows = summary.get(key) or []
        if rows:
            lines += ["", f"## {title}", ""]
            for row in rows:
                who = "|".join(
                    str(row.get(k, "")) for k in ("lane", "task", "harness", "model", "arm")
                )
                why = "; ".join(x for x in (row.get("reason"), *row.get("failures", [])) if x)
                lines.append(f"- {who}: {why}")
    lines += _render_baselines(summary.get("baselines", []))
    lines += _render_decisions(decisions)
    if summary.get("malformed"):
        lines += ["", f"{len(summary['malformed'])} malformed result record(s) were skipped."]
    return "\n".join(lines) + "\n"


def _diff_line(label: str, part: dict[str, Any]) -> str:
    if part["interval"] is None:
        return f"- {label}: no paired data"
    lo, hi = part["interval"]
    return (
        f"- {label}: {part['estimate']:+.3f} (95% interval {lo:+.3f} to {hi:+.3f}, "
        f"{part['tasks']} task strata)"
    )


def _render_baselines(baselines: list[dict[str, Any]]) -> list[str]:
    """Native-shell baseline sections. Differences favour the envelope arm when positive."""
    if not baselines:
        return []
    lines = ["", "## Baseline: native shell tool versus envelope"]
    for b in baselines:
        scope = f", tasks tagged `{b['tag']}`" if b.get("tag") else ""
        model = f", model {b['model']}" if b.get("model") else ""
        lines += ["", f"### {b['name']} ({b['claim']} claim{scope}{model})", ""]
        lines.append(
            f"`{b['versus']}` (envelope) against `{b['arm']}` (native), paired by task; positive favours envelope."
        )
        lines.append("")
        lines.append(_diff_line("success rate difference", b["success"]))
        lines.append(_diff_line("turns, relative reduction", b["turns"]))
        lines.append(_diff_line("tokens, relative reduction", b["tokens"]))
        for arm, kinds in b["failures"].items():
            lines.append(
                f"- failures of `{arm}` attributed to the tool: {kinds['tool']}, to the harness: {kinds['harness']}"
            )
    return lines


def _render_provider_split(cells: dict[str, Any]) -> list[str]:
    """Models served by more than one provider: the provider can matter more than the arm."""
    models: dict[str, set[str]] = {}
    for cell in cells.values():
        models.setdefault(cell["model"], set()).update(cell.get("providers", []))
    mixed = {m: p for m, p in models.items() if len(p) > 1}
    if not mixed:
        return []
    lines = ["", "## Providers", ""]
    for model, providers in sorted(mixed.items()):
        lines.append(
            f"- {model} was served by {', '.join(sorted(providers))}: pin one provider per model (gateway `--pins`) before comparing arms."
        )
    return lines


def _render_gaps(gaps: dict[str, Any], cells: dict[str, Any]) -> list[str]:
    """Known-gap results are listed under their gap id, counted but not mixed into the rest,
    so a later fix is measured against this baseline rather than read as a regression."""
    if not gaps:
        return []
    lines = ["", "## Known gaps", ""]
    for gap, stats in gaps.items():
        lines.append(f"### `{gap}`: {stats['successes']}/{stats['count']} passed, {_pct(stats)}")
        lines.append("")
        for name in stats["cells"]:
            cell = cells[name]
            lines.append(f"- {name}: {cell['successes']}/{cell['count']} ({_pct(cell)})")
        if stats["attributed_failures"]:
            lines.append(f"- failures by attribution: {stats['attributed_failures']}")
        lines.append("")
    return lines


def _render_decisions(decisions: dict[str, Any]) -> list[str]:
    if not decisions.get("rules"):
        return []
    lines = ["", "## Comparisons and decision rules", ""]
    for rule in decisions["rules"]:
        verdict = rule["verdict"] or "not evaluated (exploratory)"
        lines.append(f"### {rule['name']}: {verdict}")
        lines.append(f"`{rule['arm']}` versus `{rule['versus']}`, paired over tasks.")
        lines.append("")
        for part in rule["conditions"]:
            if part["interval"] is None:
                lines.append(
                    f"- {part['metric']} ({part['kind']} {part['margin']}): no paired data"
                )
                continue
            lo, hi = part["interval"]
            lines.append(
                f"- {part['metric']} ({part['kind']} {part['margin']}): "
                f"estimate {part['estimate']:+.3f}, 95% interval [{lo:+.3f}, {hi:+.3f}], "
                f"width {part['width']:.3f}, {part['tasks']} tasks"
                + (f" → {part['verdict']}" if "verdict" in part else "")
            )
        lines.append("")
    return lines
