from pathlib import Path
from typing import cast

from envelope_eval.report.aggregate import aggregate, wilson_interval
from envelope_eval.runner.databases import DatabaseAdmin
from envelope_eval.runner.planner import Arm, Job, plan_trials
from envelope_eval.runner.results import ResultWriter, read_results
from envelope_eval.tasks.loader import load_suite
from gateway.gateway import TokenRegistry, scan_credentials


def test_registry_and_scan(tmp_path: Path):
    registry = TokenRegistry()
    token = registry.issue("linux", "job", "trial", ["m"], 1)
    assert registry.get(token.token) is token
    assert registry.get("bad") is None
    assert registry.revoke(token.token)
    secret = tmp_path / "x"
    secret.write_text("prefix KEY suffix")
    assert scan_credentials(tmp_path, "KEY")[0]["offset"] == 7


def test_suite_reports_invalid_tasks(tmp_path: Path):
    suite = load_suite(tmp_path)
    assert suite.tasks == () and suite.errors == ()


def test_plan_is_reproducible_and_native_filters():
    job = Job("x", 3, ("linux",), ("a",), (Arm("default"), Arm("native", "native")), 2)
    assert plan_trials(job, {"a": {"canary"}}, {"a": True}) == plan_trials(
        job, {"a": {"canary"}}, {"a": True}
    )


def test_results_and_aggregate(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    writer = ResultWriter(path)
    writer.append(
        {"lane": "l", "task": "t", "harness": "h", "arm": "a", "passed": True, "tokens": 4}
    )
    rows, malformed = read_results(path)
    assert not malformed
    summary = aggregate(rows)
    assert summary["cells"]["l|t|h||a"]["success_rate"] == 1
    assert wilson_interval(1, 1)[0] > 0


def _task_dir(root: Path, *, tags=True, instruction="Do the work.", missing=()):
    root.mkdir()
    tag_value = '["canary"]' if tags else "[]"
    (root / "task.toml").write_text(
        f"""id="x"\nversion=1\ncategory="x"\nlanes=["linux"]\nshells=["bash"]\ntags={tag_value}\nbaseline_eligible=true\nmodifiable_paths=[]\ntimeout=10\n"""
    )
    (root / "instruction.md").write_text(instruction)
    for name in ("generate.py", "collect.py", "judge.py", "oracle"):
        if name not in missing:
            (root / name).write_text("")


def test_loader_rejects_missing_tags_and_tool_leaks(tmp_path: Path):
    from envelope_eval.tasks.loader import TaskError, load_task

    untagged = tmp_path / "untagged"
    _task_dir(untagged, tags=False)
    try:
        load_task(untagged)
    except TaskError as exc:
        assert "tag" in str(exc)
    else:
        raise AssertionError("untagged task accepted")
    leaked = tmp_path / "leaked"
    _task_dir(leaked, instruction="Use the run tool.")
    try:
        load_task(leaked)
    except TaskError as exc:
        assert "tool" in str(exc)
    else:
        raise AssertionError("tool leak accepted")
    incomplete = tmp_path / "incomplete"
    _task_dir(incomplete, missing=("judge.py",))
    try:
        load_task(incomplete)
    except TaskError as exc:
        assert "judge.py" in str(exc)
    else:
        raise AssertionError("incomplete task accepted")


def test_integrity_confinement_and_coverage(tmp_path: Path):
    from envelope_eval.tasks.execution import confinement_scan, manifest, workspace_integrity

    (tmp_path / "fixture.txt").write_text("a")
    before = manifest(tmp_path)
    (tmp_path / "fixture.txt").write_text("b")
    (tmp_path / "new.txt").write_text("c")
    result = workspace_integrity(before, tmp_path, set())
    assert result["modified"] == ["fixture.txt"] and result["created"] == ["new.txt"]
    assert confinement_scan(tmp_path, ["SECRET"]) == []
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "x").write_text("SECRET")
    assert confinement_scan(tmp_path, ["SECRET"])
    from envelope_eval.tasks import load_suite

    suite = load_suite(tmp_path / "no-tasks")
    assert suite.coverage("linux")["canary"] == []
    assert suite.coverage("linux")["background-processes"] == []
    assert "background-processes: UNCOVERED" in suite.coverage_report(("linux",))


def test_background_claim_and_default_wait_lever(tmp_path: Path):
    from envelope_eval.tasks import load_suite

    task_dir = tmp_path / "background"
    _task_dir(task_dir)
    metadata = task_dir / "task.toml"
    metadata.write_text(
        metadata.read_text().replace(
            'tags=["canary"]', 'tags=["background-processes", "default-wait"]'
        )
    )
    suite = load_suite(tmp_path)
    (task,) = suite.tasks
    assert task.claims == {"background-processes"}
    assert task.levers == {"default-wait"}
    assert suite.coverage("linux")["background-processes"] == [task.id]
    assert "background-processes: x" in suite.coverage_report(("linux",))


def test_generation_and_judging_keep_bytes(tmp_path: Path):
    from envelope_eval.tasks.execution import generate_task, judge_task

    task = tmp_path / "task"
    task.mkdir()
    (task / "generate.py").write_text(
        "def generate(out, seed):\n from pathlib import Path\n Path(out, 'cp1251.log').write_bytes('привет'.encode('cp1251'))\n return {'answer': seed}\n"
    )
    (task / "judge.py").write_text(
        "def judge(observations, answer_key, final_answer):\n return {'passed': observations['ok'] and answer_key['answer'] == 7}\n"
    )
    generated = generate_task(task, tmp_path / "fixtures", 7)
    assert generated.fixture_dir.joinpath("cp1251.log").read_bytes() == "привет".encode("cp1251")
    assert judge_task(task, {"ok": True}, generated.answer_key, "done")["passed"]


def test_generate_task_only_passes_prefix_and_db_to_generators_that_ask_for_them(tmp_path: Path):
    """Task 12.1: a database-backed generator declares `prefix`/`db` in its own signature; every
    other generator's `(out, seed)` signature is unaffected."""
    from envelope_eval.tasks.execution import generate_task

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "generate.py").write_text("def generate(out, seed):\n return {'seed': seed}\n")
    assert generate_task(plain, tmp_path / "f1", 1).answer_key == {"seed": 1}

    dbtask = tmp_path / "dbtask"
    dbtask.mkdir()
    (dbtask / "generate.py").write_text(
        "def generate(out, seed, prefix, db):\n"
        " return {'prefix': prefix, 'db': 'present' if db is not None else 'absent'}\n"
    )
    sentinel = cast(DatabaseAdmin, object())
    generated = generate_task(dbtask, tmp_path / "f2", 1, prefix="job-t00001", db=sentinel)
    assert generated.answer_key == {"prefix": "job-t00001", "db": "present"}


def test_a_declared_directory_covers_everything_below_it(tmp_path: Path):
    from envelope_eval.tasks.execution import manifest, workspace_integrity

    (tmp_path / "inbox").mkdir()
    (tmp_path / "keep").mkdir()
    for name in ("inbox/a.txt", "inbox/b.txt", "keep/c.txt", "inboxed.txt"):
        (tmp_path / name).write_text(name)
    before = manifest(tmp_path)
    (tmp_path / "inbox" / "a.txt").unlink()  # moved away
    (tmp_path / "inbox" / "b.txt").write_text("changed")
    (tmp_path / "sorted").mkdir()
    (tmp_path / "sorted" / "a.txt").write_text("a")
    ok = workspace_integrity(before, tmp_path, {"inbox"})
    assert ok["ok"] and ok["created"] == ["sorted/a.txt"]
    (tmp_path / "keep" / "c.txt").unlink()
    (tmp_path / "inboxed.txt").write_text("touched")  # a sibling that merely shares a prefix
    bad = workspace_integrity(before, tmp_path, {"inbox"})
    assert not bad["ok"] and bad["deleted"] == ["keep/c.txt"] and bad["modified"] == ["inboxed.txt"]
    assert workspace_integrity(before, tmp_path, {"inbox", "keep", "inboxed.txt"})["ok"]
