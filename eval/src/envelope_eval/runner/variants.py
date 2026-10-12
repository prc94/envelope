"""Build reproducible envelope.pyz variants from small, reviewable patches."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildVariant:
    name: str
    source_hash: str
    artifact_hash: str
    artifact: Path


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_variant(
    repo: str | Path,
    patch: str | Path | None = None,
    *,
    name: str = "variant",
    output: str | Path | None = None,
    revision: str | None = None,
) -> BuildVariant:
    repo = Path(repo).resolve()
    with tempfile.TemporaryDirectory(prefix="envelope-variant-") as directory:
        staging = Path(directory) / "repo"
        if revision is None:
            shutil.copytree(
                repo,
                staging,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".codegraph",
                    ".serena",
                    ".pytest_cache",
                    ".ruff_cache",
                    "__pycache__",
                    "*.pyc",
                    "dist",
                    "eval/results",
                ),
            )
        else:
            archive = subprocess.run(
                ["git", "archive", "--format=tar", revision],
                cwd=repo,
                check=True,
                capture_output=True,
            ).stdout
            staging.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(staging, filter="data")
        if patch is not None:
            patch_path = Path(patch)
            subprocess.run(
                ["patch", "-p1", "--batch", "--forward", "-i", str(patch_path.resolve())],
                cwd=staging,
                check=True,
                capture_output=True,
            )
        source_hash = _tree_hash(staging / "envelope")
        # Absolute: the build runs from a temporary staging directory, so a relative path would land there.
        artifact = (
            Path(output) if output is not None else repo / "eval" / "results" / f"{name}.pyz"
        ).resolve()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["python3", str(staging / "scripts" / "build.py"), "--output", str(artifact)],
            cwd=staging,
            check=True,
            capture_output=True,
        )
        artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return BuildVariant(name, source_hash, artifact_hash, artifact)
