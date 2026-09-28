"""Host-side task generation, byte-preserving upload and integrity checks."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import runpy
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..runner.databases import DatabaseAdmin


@dataclass(frozen=True)
class Generation:
    seed: int
    fixture_dir: Path
    answer_key: Any
    manifest: dict[str, str]


def render_instruction(template: str, answer_key: Any) -> str:
    """Fill `<<name>>` placeholders from the answer key's `params` (per-trial numbers and names),
    so expected values differ between trials and cannot be memorized. Unknown placeholders are
    an error: an instruction must never reach an agent half-rendered."""
    params = answer_key.get("params", {}) if isinstance(answer_key, dict) else {}

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"instruction placeholder <<{name}>> has no value in the answer key")
        return str(params[name])

    return re.sub(r"<<([A-Za-z_][A-Za-z0-9_]*)>>", fill, template)


def manifest(root: str | Path) -> dict[str, str]:
    root = Path(root)
    result = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts and p.relative_to(root).parts[0] != "scratch":
            result[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def upload_bytes(source: str | Path, target: str | Path) -> None:
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as inp, target.open("wb") as out:
        shutil.copyfileobj(inp, out)
    shutil.copystat(source, target)


def _allowed(path: str, modifiable: set[str]) -> bool:
    """A declared path may name a file or a directory (everything below it)."""
    return any(path == m or path.startswith(m.rstrip("/") + "/") for m in modifiable)


def workspace_integrity(
    before: dict[str, str], after_root: str | Path, modifiable: set[str]
) -> dict[str, Any]:
    """Fixture files the agent may not touch must be unchanged: modified or deleted ones outside
    the declared paths fail the trial; files it created are only reported."""
    after = manifest(after_root)
    modified = sorted(
        path
        for path, digest in before.items()
        if after.get(path) != digest and path in after and not _allowed(path, modifiable)
    )
    deleted = sorted(p for p in set(before) - set(after) if not _allowed(p, modifiable))
    created = sorted(set(after) - set(before))
    return {
        "modified": modified,
        "deleted": deleted,
        "created": created,
        "ok": not modified and not deleted,
    }


def run_collect(task: Path, workspace: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(task / "collect.py")],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"collect failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def confinement_scan(root: str | Path, secrets: list[str]) -> list[str]:
    found = []
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(secret.encode() in data for secret in secrets):
            found.append(str(path))
    return found


def generate_task(
    task_root: str | Path,
    fixture_dir: str | Path,
    seed: int,
    *,
    prefix: str | None = None,
    db: DatabaseAdmin | None = None,
) -> Generation:
    """Run a task generator on the host and retain answer key and manifest locally.

    Most generators only take `(out, seed)`. A generator whose data lives in a shared database
    (task 12.1) additionally declares `prefix` (the trial id, its database/ACL namespace) and
    `db` (the `DatabaseAdmin` to provision through); passed only when the generator asks for
    them, by name, so every other generator's signature is unaffected."""
    task_root = Path(task_root)
    fixture_dir = Path(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    generator = task_root / "generate.py"
    sys.path.insert(0, str(task_root.parent))  # `from _lib.gen import ...`
    try:
        namespace = runpy.run_path(str(generator), run_name="__task_generator__")
    finally:
        sys.path.remove(str(task_root.parent))
    generate = namespace.get("generate")
    if not callable(generate):
        raise TypeError("generator must define generate(out, seed)")
    params = inspect.signature(generate).parameters
    kwargs: dict[str, Any] = {}
    if "prefix" in params:
        kwargs["prefix"] = prefix
    if "db" in params:
        kwargs["db"] = db
    answer_key = generate(str(fixture_dir), seed, **kwargs)
    return Generation(seed, fixture_dir, answer_key, manifest(fixture_dir))


def judge_task(
    task_root: str | Path, observations: dict[str, Any], answer_key: Any, final_answer: str
) -> dict[str, Any]:
    """Run a host-side judge; checker failures are infrastructure failures."""
    task_root = Path(task_root)
    judge_path = task_root / "judge.py"
    namespace = runpy.run_path(str(judge_path), run_name="__task_judge__")
    judge = namespace.get("judge")
    if not callable(judge):
        raise TypeError("judge must define judge(observations, answer_key, final_answer)")
    result = judge(observations, answer_key, final_answer)
    if not isinstance(result, dict) or "passed" not in result:
        raise RuntimeError("judge must return a dict containing passed")
    return result
