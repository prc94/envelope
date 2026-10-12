"""Oracle steps, the instruction's code block, and the oracle gate."""

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from test_trial_lifecycle import CANARY, FakeTarget, UnitResult, transcript

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


def test_multi_call_oracle_preserves_server_handle_and_probe_answer(tmp_path, monkeypatch):
    script = tmp_path / "oracle.json"
    script.write_text(
        json.dumps(
            [
                {"tool": "run", "arguments": {"command": "serve"}, "save_as": "server"},
                {"tool": "output", "arguments": {"handle": "$server", "wait_for": "regex:ready"}},
                {"tool": "run", "arguments": {"command": "probe"}, "answer_from": True},
                {"tool": "stop", "arguments": {"handle": "$server"}},
            ]
        )
    )

    class FakeServer:
        calls: ClassVar[list[tuple[str, dict]]] = []

        def __init__(self, _pyz):
            pass

        def call(self, method, params=None):
            if method == "initialize":
                return {}
            if method == "tools/list":
                return {"tools": [{"name": name} for name in launcher_oracle.TOOLS_BY_NAME]}
            assert method == "tools/call"
            assert params is not None
            self.calls.append((params["name"], params["arguments"]))
            name = params["name"]
            if name == "run":
                handle = "out_server" if params["arguments"]["command"] == "serve" else "out_probe"
                return {
                    "content": [{"text": "exit 0 | bash\nTOKEN=42"}],
                    "structuredContent": {"handle": handle},
                }
            return {"content": [{"text": "done"}], "structuredContent": {}}

        def notify(self, _method):
            pass

        def close(self):
            pass

    events = []
    monkeypatch.setattr(launcher_oracle, "Server", FakeServer)
    monkeypatch.setattr(launcher_oracle, "emit", events.append)
    assert launcher_oracle.main(["--envelope", "unused", "--oracle", str(script)]) == 0
    assert [name for name, _ in FakeServer.calls] == ["run", "output", "run", "stop"]
    assert FakeServer.calls[1][1]["handle"] == FakeServer.calls[3][1]["handle"] == "out_server"
    assert events[-1]["result"] == "TOKEN=42"


@pytest.mark.skipif(not (Path("dist") / "envelope.pyz").exists(), reason="needs dist/envelope.pyz")
def test_dev_server_probe_oracle_on_linux(tmp_path):
    import importlib.util
    import os
    import subprocess

    from envelope_eval.tasks.execution import render_instruction

    root = Path("eval/tasks/dev-server-probe").resolve()
    spec = importlib.util.spec_from_file_location("dev_server_generator", root / "generate.py")
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    answer_key = generator.generate(str(tmp_path), 1)
    oracle = tmp_path / "oracle.json"
    oracle.write_text(render_instruction((root / "oracle").read_text(), answer_key))
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(Path(launcher_oracle.__file__)),
            "--envelope",
            str(Path("dist/envelope.pyz").resolve()),
            "--oracle",
            str(oracle),
            "--shell",
            "bash",
            "--cwd",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = (tmp_path / "hits.log").read_text().split()[-1]
    assert json.loads(result.stdout.splitlines()[-1])["result"] == value


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
        "mcp__envelope__stop",
        "mcp__envelope__processes",
    }


def qwen_shaped_harness(answer):
    """What launcher/oracle.py leaves behind, without running it."""

    def harness(target, ws, stdout_to):
        transcript(
            stdout_to,
            {"type": "system", "subtype": "init", "tools": launcher_oracle.TOOLS},
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
