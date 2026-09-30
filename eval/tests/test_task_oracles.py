"""Every task's oracle must solve it: generate fixtures, run the oracle through the launcher and a
real envelope on this host, and pass the task's own judge (the Windows VM is checked separately).

Tasks whose generator needs a `DatabaseAdmin` (task 13.1's Mongo/Redis tasks) are excluded here:
they need the shared Mongo/Redis containers on `envelope-linux`, not available to this quick,
server-free host check; their oracles are verified live against those containers instead (see
design.md's task 13.1 entry), the same way the Windows-only tasks are checked on the VM."""

import inspect
import os
import runpy
import shutil
import sys
from pathlib import Path

import pytest

from envelope_eval.runner.oracle import OracleRunner
from envelope_eval.targets import LocalTrialTarget
from envelope_eval.tasks import load_suite

ROOT = Path(__file__).parents[2]
SUITE = load_suite(ROOT / "eval" / "tasks")
PYZ = ROOT / "dist" / "envelope.pyz"


def _needs_db(task) -> bool:
    sys.path.insert(0, str(task.root.parent))
    try:
        namespace = runpy.run_path(str(task.root / "generate.py"), run_name="__probe__")
    finally:
        sys.path.remove(str(task.root.parent))
    return "db" in inspect.signature(namespace["generate"]).parameters


LOCAL = [t.id for t in SUITE.tasks if "linux" in t.lanes and not _needs_db(t)]

pytestmark = pytest.mark.skipif(
    not PYZ.exists() or shutil.which("git") is None or not sys.platform.startswith("linux"),
    reason="needs dist/envelope.pyz, git and Linux",
)


def test_the_suite_loads_without_errors():
    assert SUITE.errors == ()
    assert {t.id for t in SUITE.tasks} >= {"canary", "git-log-ticket", "gitbash-cp866-dir"}


@pytest.mark.parametrize(
    "task_id",
    [pytest.param(t, marks=pytest.mark.slow) if t == "interop-long" else t for t in LOCAL],
)
def test_oracle_passes_its_own_judge(task_id, tmp_path, monkeypatch):
    # Oracles pick `python` (or `python3`) from PATH, as they do on the targets: give them this one.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    runner = OracleRunner(
        LocalTrialTarget(tmp_path / "trials"),
        {t.id: t for t in SUITE.tasks},
        tmp_path / "scratch",
        sys.executable,
        str(ROOT / "eval" / "launcher" / "oracle.py"),
        str(PYZ),
        shell="bash",
    )
    record = runner.run(task_id, seed=5)
    assert record["passed"], (record.get("judge"), record.get("error"), record.get("attribution"))
    assert record["leftover_processes"] == []
