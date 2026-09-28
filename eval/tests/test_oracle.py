"""Oracle steps, the instruction's code block, and the oracle gate."""

import json
import sys
from pathlib import Path

import pytest
from test_trial_lifecycle import CANARY, TOOLS, FakeTarget, UnitResult, transcript

from envelope_eval.runner.oracle import OracleRunner
from envelope_eval.runner.planner import Trial
from envelope_eval.tasks import load_task
from launcher import oracle as launcher_oracle

INSTRUCTION = """Use the tool to run exactly this:

```
seq 1 20000 | awk '{print "row " $1} NR==7 {print "TOKEN=" 7*11}'
```

Then reply."""


def test_code_block_is_the_first_fenced_block_verbatim():
    code = launcher_oracle.code_block(INSTRUCTION)
    assert code == """seq 1 20000 | awk '{print "row " $1} NR==7 {print "TOKEN=" 7*11}'"""
    with pytest.raises(RuntimeError, match="no fenced code block"):
        launcher_oracle.code_block("nothing here")


def test_a_plain_oracle_is_one_run_step_and_placeholders_survive_json_escaping():
    (step,) = launcher_oracle.load_steps("{{CODE}}\n", INSTRUCTION)
    assert step["tool"] == "run"
    assert step["arguments"]["command"].startswith('seq 1 20000 | awk \'{print "row "')
    assert step["arguments"]["command"].strip().endswith("7*11}'")
    (plain,) = launcher_oracle.load_steps("printf canary", "")
    assert plain["arguments"]["command"] == "printf canary"


def test_a_json_oracle_lists_steps_and_keeps_the_handle_placeholder():
    text = json.dumps(
        [
            {"tool": "run", "arguments": {"command": "{{CODE}}"}},
            {"tool": "output", "arguments": {"handle": "$handle", "grep": "TOKEN="}},
        ]
    )
    run, output = launcher_oracle.load_steps(text, INSTRUCTION)
    assert "TOKEN=" in run["arguments"]["command"] and output["arguments"]["handle"] == "$handle"


@pytest.mark.skipif(not (Path("dist") / "envelope.pyz").exists(), reason="needs dist/envelope.pyz")
def test_the_oracle_runner_drives_a_real_envelope_and_the_transcript_parses(tmp_path):
    import subprocess

    from envelope_eval.adapters.qwen import QwenAdapter

    home = tmp_path / "home"
    home.mkdir()
    (home / "oracle.txt").write_text(
        json.dumps(
            [
                {"tool": "run", "arguments": {"command": "{{CODE}}"}},
                {"tool": "output", "arguments": {"handle": "$handle", "grep": "TOKEN="}},
            ]
        )
    )
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_STATE_HOME": str(tmp_path / "s"),
        "XDG_CACHE_HOME": str(tmp_path / "c"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(launcher_oracle.__file__)),
            "--envelope",
            str(Path("dist/envelope.pyz").resolve()),
            "--oracle-in-home",
            "oracle.txt",
            "--instruction",
            INSTRUCTION,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(proc.stdout, encoding="utf-8")
    trajectory = QwenAdapter().parse_trajectory(transcript)
    calls = [e["name"] for e in trajectory.events if e["type"] == "tool_call"]
    assert calls == ["run", "output"] and not trajectory.truncated
    assert "TOKEN=77" in trajectory.final_answer
    assert QwenAdapter().exposed_tools(trajectory) == {
        "mcp__envelope__run",
        "mcp__envelope__output",
    }


def qwen_shaped_harness(answer):
    """What launcher/oracle.py leaves behind, without running it."""

    def harness(target, ws, stdout_to):
        transcript(
            stdout_to,
            {"type": "system", "subtype": "init", "tools": list(TOOLS)},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]},
            },
            {"type": "result", "subtype": "success", "is_error": False, "result": answer},
        )
        return UnitResult(0, False)

    return harness


def test_the_oracle_gate_reports_pass_and_the_reason_for_a_failure(tmp_path):
    tasks = {"canary": load_task(CANARY)}
    for harness, expect_ok in (
        (qwen_shaped_harness("canary"), True),
        (qwen_shaped_harness("nope"), False),
    ):
        target = FakeTarget(tmp_path / f"remote-{expect_ok}", harness)
        runner = OracleRunner(
            target, tasks, tmp_path / "scratch", "python", "oracle.py", "envelope.pyz"
        )
        ok, reason = runner.check()(Trial(0, "l", "canary", "oracle", 1))
        assert ok is expect_ok
        assert expect_ok or reason  # a failing oracle says why
        assert runner.results["canary"]["passed"] is expect_ok
