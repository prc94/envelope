"""Long-job judges score the answer, not the wait path (a long `wait_s`, or a native shell with no
audit log, follows the job just as well)."""

import importlib.util
from pathlib import Path

import pytest

TASKS = Path(__file__).parents[1] / "tasks"
NO_AUDIT = {"handed_back": False, "expired": False, "finished": False, "continued": False}


def judge_of(task):
    spec = importlib.util.spec_from_file_location(f"judge_{task}", TASKS / task / "judge.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.judge


@pytest.mark.parametrize("task", ["long-wait", "build-outlives-wait"])
def test_the_final_line_passes_whatever_path_led_to_it(task):
    judge = judge_of(task)
    key = {"expect": "DONE 8f3a"}
    assert judge(NO_AUDIT, key, "The final line was: DONE 8f3a")["passed"]
    assert not judge(NO_AUDIT, key, "It is still running.")["passed"]


def test_watch_rebuild_accepts_the_result_of_whatever_bytes_the_edit_wrote():
    judge = judge_of("watch-rebuild")
    for source in ("version=2\n", "version=2", "version=2\r\n"):
        obs = {"source": source, "result": f"RESULT {hash(source) & 0xFFFF:04x}", "reads": 0}
        assert judge(obs, {}, f"New result: {obs['result']}")["passed"], repr(source)
    assert not judge({"source": "version=1\n", "result": "RESULT a", "reads": 3}, {}, "RESULT a")[
        "passed"
    ]


def test_restart_after_edit_needs_both_versions_served_not_the_restart_parameter():
    judge = judge_of("restart-after-edit")
    key = {"expect": "version=2"}
    native = {"config": "version=2", "hits": ["version=1", "version=2"], "restarts": 0}
    assert judge(native, key, "version=2")["passed"]
    never_restarted = {"config": "version=2\n", "hits": ["version=1", "version=1"], "restarts": 0}
    assert not judge(never_restarted, key, "version=2")["passed"]


def test_port_in_use_scores_the_owner_identity_without_the_hint():
    judge = judge_of("port-in-use")
    assert judge({"identity": "owner-1a2b", "port_hint": False}, {}, "owner-1a2b\n")["passed"]
    assert not judge({"identity": "owner-1a2b", "port_hint": True}, {}, "contender")["passed"]
