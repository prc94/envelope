"""Task metadata validation and suite hashing."""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_FILES = ("task.toml", "instruction.md", "generate.py", "collect.py", "judge.py", "oracle")
REQUIRED_FIELDS = (
    "id",
    "version",
    "category",
    "lanes",
    "shells",
    "tags",
    "baseline_eligible",
    "modifiable_paths",
    "timeout",
)
TOOL_NAMES = {"run", "output", "input", "stop", "tool_search", "run_shell_command"}


class TaskError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    root: Path
    id: str
    version: str
    category: str
    lanes: tuple[str, ...]
    shells: tuple[str, ...]
    tags: tuple[str, ...]
    baseline_eligible: bool
    known_gaps: tuple[str, ...]
    modifiable_paths: tuple[str, ...]
    timeout: float
    instruction: str

    @property
    def hash(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and ".git" not in path.parts:
                digest.update(path.relative_to(self.root).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def validate_instruction(self) -> None:
        text = self.instruction
        if self.baseline_eligible:
            lowered = text.lower()
            for name in TOOL_NAMES:
                if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", lowered):
                    raise TaskError(f"baseline instruction names tool {name!r}")
            if "windows" in self.lanes and re.search(r"```(?:bash|sh)\b", lowered):
                raise TaskError("Windows baseline task contains a fenced bash command")


def _field(data: dict[str, Any], name: str, typ: type | tuple[type, ...]) -> Any:
    if name not in data:
        raise TaskError(f"missing metadata field {name}")
    value = data[name]
    if not isinstance(value, typ):
        names = " or ".join(t.__name__ for t in (typ if isinstance(typ, tuple) else (typ,)))
        raise TaskError(f"field {name} must be {names}")
    return value


def load_task(root: str | Path) -> Task:
    root = Path(root)
    if not root.is_dir():
        raise TaskError(f"task directory not found: {root}")
    missing = [name for name in REQUIRED_FILES if not (root / name).exists()]
    if missing:
        raise TaskError(f"missing task parts: {', '.join(missing)}")
    try:
        data = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"invalid task.toml: {exc}") from exc
    task = Task(
        root=root,
        id=_field(data, "id", str),
        version=str(_field(data, "version", (str, int))),
        category=_field(data, "category", str),
        lanes=tuple(_field(data, "lanes", list)),
        shells=tuple(_field(data, "shells", list)),
        tags=tuple(_field(data, "tags", list)),
        baseline_eligible=_field(data, "baseline_eligible", bool),
        known_gaps=tuple(data.get("known_gaps", [])),
        modifiable_paths=tuple(_field(data, "modifiable_paths", list)),
        timeout=float(_field(data, "timeout", (int, float))),
        instruction=(root / "instruction.md").read_text(encoding="utf-8"),
    )
    if not task.tags:
        raise TaskError("task must declare at least one tag")
    if not task.lanes or not task.shells:
        raise TaskError("lanes and shells must not be empty")
    task.validate_instruction()
    return task


@dataclass(frozen=True)
class TaskSuite:
    tasks: tuple[Task, ...]
    hash: str
    errors: tuple[str, ...] = ()

    def for_lane(self, lane: str) -> tuple[Task, ...]:
        return tuple(t for t in self.tasks if lane in t.lanes)

    def coverage(self, lane: str) -> dict[str, list[str]]:
        required = (
            "canary",
            "large-output",
            "long-running",
            "errors",
            "hints",
            "heavy-scripts",
            "cwd-env",
        )
        selected = self.for_lane(lane)
        return {tag: [task.id for task in selected if tag in task.tags] for tag in required}


def load_suite(root: str | Path) -> TaskSuite:
    root = Path(root)
    tasks: list[Task] = []
    errors: list[str] = []
    for path in (
        sorted(p for p in root.iterdir() if p.is_dir() and p.name[0] not in ".__")
        if root.exists()
        else []
    ):
        try:
            tasks.append(load_task(path))
        except TaskError as exc:
            errors.append(f"{path.name}: {exc}")
    digest = hashlib.sha256()
    for task in tasks:
        digest.update(task.id.encode())
        digest.update(task.hash.encode())
    # Generators share helpers in `_lib`; editing one changes every task's fixtures.
    for path in sorted((root / "_lib").rglob("*.py")) if (root / "_lib").exists() else []:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return TaskSuite(tuple(tasks), digest.hexdigest(), tuple(errors))
