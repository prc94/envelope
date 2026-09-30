"""Trial lifecycle against a fake target, harness, adapter and gateway."""

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from envelope_eval.adapters.base import Trajectory
from envelope_eval.runner.lifecycle import Gates, TrialRunner
from envelope_eval.runner.planner import Arm, Trial
from envelope_eval.runner.results import ResultWriter, read_results
from envelope_eval.runner.trial import TrialExecutor, UnitResult, Workspace, trial_id
from envelope_eval.tasks import load_task

TOOLS = ("mcp__envelope__run", "mcp__envelope__output")
CANARY = Path(__file__).parents[1] / "tasks" / "canary"


class FakeGateway:
    def __init__(self):
        self.issued: list[str] = []
        self.revoked: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.in_flight = False

    def issue(self, **kwargs):
        token = f"tok-{len(self.issued)}"
        self.issued.append(token)
        self.last_issue = kwargs
        return token

    def revoke(self, token):
        self.revoked.append(token)

    def state(self, token):
        return {"in_flight": self.in_flight}

    def ledger(self, trial):
        return [r | {"token": {"trial": trial}} for r in self.rows]


class FakeAdapter:
    name = "fake"

    def configure(self, home, model, token, tool_set, base_url, env):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps([model, token, tool_set, base_url]))

    def command(self, instruction, workdir, kind, tool_set):
        return ["fake-harness", instruction]

    def environment(self, model, token, base_url):
        return {"FAKE_MODEL": model, "FAKE_TOKEN": token, "FAKE_BASE_URL": base_url}

    def implicit_tools(self, kind):
        return set()

    def visible_tools(self, kind, tool_set):
        return None

    def parse_trajectory(self, transcript):
        events, truncated = [], False
        for line in transcript.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                truncated = True
        final = next((e["content"] for e in reversed(events) if e.get("type") == "final"), "")
        return Trajectory(tuple(events), final, truncated)

    def exposed_tools(self, trajectory):
        for event in trajectory.events:
            if event.get("type") == "init":
                return set(event["tools"])
        return None


class FakeTarget:
    """A 'remote' host that is a directory tree; the harness is a callable."""

    def __init__(self, root: Path, harness: Callable[["FakeTarget", Workspace, str], UnitResult]):
        self.root = root
        self.harness = harness
        self.events: list[str] = []
        self.fail_upload = False
        self.units: list[dict[str, Any]] = []

    def prepare(self, trial_id):
        self.events.append(f"prepare {trial_id}")
        base = self.root / trial_id
        dirs = {n: base / n for n in ("workspace", "home", "state", "spool")}
        for d in dirs.values():
            d.mkdir(parents=True)
        return Workspace(
            workspace=str(dirs["workspace"]),
            home=str(dirs["home"]),
            state=str(dirs["state"]),
            audit_log=str(dirs["state"] / "audit.jsonl"),
            spool=str(dirs["spool"]),
            env={"HOME": str(dirs["home"])},
        )

    def real_path(self, remote):
        return remote

    def upload(self, local, remote):
        if self.fail_upload:
            raise ConnectionError("ssh: connection reset")
        shutil.copytree(local, remote, dirs_exist_ok=True)

    def download(self, remote, local):
        source = Path(remote)
        if not source.exists():
            raise FileNotFoundError(remote)
        if source.is_dir():
            shutil.copytree(source, local, dirs_exist_ok=True)
        else:
            shutil.copy(source, local)

    def run_unit(self, argv, *, env, cwd, timeout, stdout_to):
        self.events.append("unit")
        self.units.append({"argv": argv, "env": env, "cwd": cwd, "timeout": timeout})
        state = Path(stdout_to).parent
        ws = Workspace(cwd, env["HOME"], str(state), str(state / "audit.jsonl"), "")
        return self.harness(self, ws, stdout_to)

    def collect(self, script, ws, timeout):
        self.events.append("collect")
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(script)],
            cwd=ws.workspace,
            capture_output=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def cleanup(self, ws):
        self.events.append("cleanup")
        shutil.rmtree(Path(ws.workspace).parent, ignore_errors=True)


def transcript(path, *events):
    Path(path).write_text("".join(json.dumps(e) + "\n" for e in events))


def good_harness(answer="canary", tools=TOOLS, exit_code=0, leftovers=()):
    def harness(target, ws, stdout_to):
        transcript(
            stdout_to,
            {"type": "init", "tools": list(tools)},
            {"type": "tool_call", "name": "run", "id": "1"},
            {"type": "tool_result", "id": "1"},
            {"type": "final", "content": answer},
        )
        return UnitResult(exit_code, False, tuple(leftovers))

    return harness


def make(tmp_path, harness, arm_kind="envelope", tasks=None):
    gateway, adapter = FakeGateway(), FakeAdapter()
    gateway.rows = [
        {
            "role": "main",
            "timestamp": 100.0,
            "duration": 2.0,
            "end_reason": "complete",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.25},
        }
    ] * 2
    target = FakeTarget(tmp_path / "remote", harness)
    arm = Arm("default", kind=arm_kind, tool_set=TOOLS)
    executor = TrialExecutor(
        target,
        adapter,
        gateway,
        tasks or {"canary": load_task(CANARY)},
        {"default": arm},
        job="job",
        gateway_url="http://127.0.0.1:9/v1",
        scratch=tmp_path / "scratch",
    )
    return executor, target, gateway


def trial(index=0, model="m", task="canary", target="vm"):
    return Trial(index, "windows", task, "default", 100 + index, target, "fake", model)


def test_probe_trial_ids_are_unique_per_cell_not_just_per_index():
    # Gates._run_canaries always probes with index -1/-2 regardless of which cell (harness+model)
    # it is checking; since the gateway's ledger is keyed by trial id alone, two cells sharing a
    # probe index must still get different ids or one cell's canary would read another's ledger
    # rows back (invisible with one harness, since every probe then expects the same tools; a
    # real bug once cells can expect different ones -- task 14.1).
    qwen = Trial(-1, "linux", "canary", "default", 1, "t", "qwen-code", "m")
    opencode = Trial(-1, "linux", "canary", "default", 1, "t", "opencode", "m")
    assert trial_id("job", qwen) != trial_id("job", opencode)
    other_model = Trial(-1, "linux", "canary", "default", 1, "t", "qwen-code", "other-m")
    assert trial_id("job", qwen) != trial_id("job", other_model)
    # Real trials (index >= 0) are already unique by index alone; format is unchanged.
    assert trial_id("job", trial(0)) == "job-t00000"


def test_full_lifecycle_passes_and_cleans_up(tmp_path):
    executor, target, gateway = make(tmp_path, good_harness())
    result = executor(trial())
    assert result["status"] == "pass" and result["passed"] and result["attribution"] is None
    assert result["turns"] == 2 and result["tokens"] == 30 and result["cost"] == 0.5
    assert result["tool_sequence"] == ["run"] and result["tool_set"] == sorted(TOOLS)
    assert result["leftover_processes"] == [] and result["leftovers"] == 0
    assert result["integrity"]["ok"] and result["created_files"] == 0
    assert target.events == ["prepare job-t00000", "unit", "collect", "cleanup"]
    assert gateway.revoked == gateway.issued == ["tok-0"]
    assert result["trial_secrets"] == ["tok-0"]
    assert gateway.last_issue["allowed_models"] == ["m"] and gateway.last_issue["lane"] == "windows"
    (unit,) = target.units
    assert unit["timeout"] == load_task(CANARY).timeout and unit["argv"][0] == "fake-harness"
    assert not (tmp_path / "scratch" / "job-t00000").exists()
    assert not (tmp_path / "remote" / "job-t00000").exists()
    json.dumps(result)  # the record must be serializable as is


def test_wrong_answer_is_a_model_failure(tmp_path):
    executor, _, _ = make(tmp_path, good_harness(answer="nope"))
    result = executor(trial())
    assert result["status"] == "fail" and result["attribution"] == "model"


def test_timeout_with_command_in_flight_is_a_tool_failure(tmp_path):
    def hang(target, ws, stdout_to):
        transcript(stdout_to, {"type": "init", "tools": list(TOOLS)})
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        Path(ws.audit_log).write_text(json.dumps({"ts": stamp, "event": "decision"}) + "\n")
        return UnitResult(None, True, ("pid 4242 sleep",))

    executor, _, gateway = make(tmp_path, hang)
    gateway.rows = []
    result = executor(trial())
    assert result["timed_out"] and not result["passed"]
    assert result["attribution"] == "tool"
    assert result["leftover_processes"] == ["pid 4242 sleep"] and result["leftovers"] == 1
    assert any(e["kind"] == "timeout" for e in result["timeline"])
    assert gateway.revoked == ["tok-0"]


def test_idle_harness_timeout_is_a_harness_failure(tmp_path):
    def idle(target, ws, stdout_to):
        transcript(stdout_to, {"type": "init", "tools": list(TOOLS)})
        return UnitResult(None, True)

    executor, _, gateway = make(tmp_path, idle)
    gateway.rows = []
    assert executor(trial())["attribution"] == "harness"


def test_tool_set_mismatch_is_infra_and_not_scored(tmp_path):
    extra = (*TOOLS, "read_file")
    executor, target, gateway = make(tmp_path, good_harness(tools=extra))
    result = executor(trial())
    assert result["status"] == "infra" and result["passed"] is False
    assert "unexpected ['read_file']" in result["error"]
    assert "collect" not in target.events  # never reached scoring
    assert gateway.revoked and target.events[-1] == "cleanup"


def test_missing_tool_report_is_infra(tmp_path):
    def silent(target, ws, stdout_to):
        transcript(stdout_to, {"type": "final", "content": "canary"})
        return UnitResult(0, False)

    executor, _, _ = make(tmp_path, silent)
    assert "no report" in executor(trial())["error"]


def test_setup_failure_is_infra_with_cleanup_and_revocation(tmp_path):
    executor, target, gateway = make(tmp_path, good_harness())
    target.fail_upload = True
    result = executor(trial())
    assert result["attribution"] == "infra" and "upload fixtures" in result["error"]
    assert "unit" not in target.events and target.events[-1] == "cleanup"
    assert gateway.revoked == ["tok-0"]


def test_collect_crash_is_infra(tmp_path):
    bad = tmp_path / "tasks" / "bad"
    shutil.copytree(CANARY, bad)
    (bad / "collect.py").write_text("raise SystemExit(3)")
    executor, _, _ = make(tmp_path, good_harness(), tasks={"canary": load_task(bad)})
    result = executor(trial())
    assert result["attribution"] == "infra" and result["error"].startswith("collect:")


def test_judge_crash_is_infra(tmp_path):
    bad = tmp_path / "tasks" / "bad"
    shutil.copytree(CANARY, bad)
    (bad / "judge.py").write_text("def judge(o, k, f):\n    raise KeyError('x')")
    executor, _, _ = make(tmp_path, good_harness(), tasks={"canary": load_task(bad)})
    assert executor(trial())["error"].startswith("judge:")


def test_modifying_a_fixture_fails_even_when_the_answer_is_right(tmp_path):
    task = tmp_path / "tasks" / "fixture"
    shutil.copytree(CANARY, task)
    (task / "generate.py").write_text(
        "from pathlib import Path\n"
        "def generate(out, seed):\n"
        "    Path(out).mkdir(parents=True, exist_ok=True)\n"
        "    (Path(out) / 'data.txt').write_text('original')\n"
        "    return {}\n"
    )

    def vandal(target, ws, stdout_to):
        (Path(ws.workspace) / "data.txt").write_text("changed")
        return good_harness()(target, ws, stdout_to)

    executor, _, _ = make(tmp_path, vandal, tasks={"canary": load_task(task)})
    result = executor(trial())
    assert result["integrity"]["modified"] == ["data.txt"]
    assert result["status"] == "fail" and result["attribution"] == "model"


def test_baseline_arm_takes_tool_events_from_the_trajectory(tmp_path):
    executor, _, _ = make(tmp_path, good_harness(), arm_kind="native")
    result = executor(trial())
    assert result["passed"]
    kinds = [e["kind"] for e in result["timeline"] if e["kind"].startswith("tool_")]
    assert kinds == ["tool_start", "tool_end"]


def test_runner_retries_only_infra_and_the_retry_replaces_the_attempt(tmp_path):
    executor, target, _ = make(tmp_path, good_harness())
    target.fail_upload = True
    writer = ResultWriter(tmp_path / "results.jsonl")
    runner = TrialRunner(writer)

    calls = []

    def flaky(t):
        calls.append(1)
        target.fail_upload = len(calls) < 3
        return executor(t)

    record = runner.run_trial(trial(), flaky)
    assert record["passed"] and record["retries"] == 2 and len(record["retry_reasons"]) == 2
    assert all("upload fixtures" in reason for reason in record["retry_reasons"])
    rows, malformed = read_results(tmp_path / "results.jsonl")
    assert len(rows) == 1 and not malformed  # one record per trial, not per attempt


def test_runner_gives_up_after_two_retries_and_never_retries_model_failures(tmp_path):
    writer = ResultWriter(tmp_path / "results.jsonl")
    runner = TrialRunner(writer)
    attempts = []

    def always_infra(t):
        attempts.append(1)
        raise ConnectionError("tunnel down")

    record = runner.run_trial(trial(), always_infra)
    assert len(attempts) == 3 and record["retries"] == 2 and record["attribution"] == "infra"

    failing = []

    def model_failure(t):
        failing.append(1)
        return {"status": "fail", "passed": False, "attribution": "model"}

    record = runner.run_trial(trial(1), model_failure)
    assert len(failing) == 1 and record["retries"] == 0


def test_circuit_breaker_is_per_cell_and_needs_three_consecutive_infra_trials(tmp_path):
    runner = TrialRunner(ResultWriter(tmp_path / "results.jsonl"))
    infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "502"}
    ok = {"status": "pass", "passed": True}
    runner.max_retries = 0
    runner.run_trial(trial(0), lambda t: infra)
    runner.run_trial(trial(1), lambda t: infra)
    runner.run_trial(trial(2), lambda t: ok)  # a success resets the count
    for i in (3, 4, 5):
        runner.run_trial(trial(i), lambda t: infra)
    ran = []
    blocked = runner.run_trial(trial(6), lambda t: ran.append(1) or ok)
    assert blocked["status"] == "unavailable" and ran == []
    assert blocked["failures"] == ["502", "502", "502"]
    other = runner.run_trial(trial(7, model="other"), lambda t: ok)
    assert other["passed"]  # a different model's cell is unaffected


def test_oracle_gate_marks_a_task_broken_without_running_it(tmp_path):
    runner = TrialRunner(ResultWriter(tmp_path / "results.jsonl"))
    ran = []

    def execute(t):
        ran.append(t.task)
        return {"status": "pass", "passed": True}

    gates = Gates(lambda t: (t.task != "b", "oracle exited 1"), execute)
    assert runner.run_trial(trial(0, task="a"), execute, gate=gates)["passed"]
    broken = runner.run_trial(trial(1, task="b"), execute, gate=gates)
    assert broken["status"] == "broken" and "oracle" in broken["reason"]
    assert ran == ["canary", "a"]  # one passing canary is enough; b never ran
    runner.run_trial(trial(2, task="a"), execute, gate=gates)  # oracle result is cached
    assert ran.count("a") == 2


def test_canary_infra_failures_make_the_cell_unavailable_and_skip_its_trials(tmp_path):
    writer = ResultWriter(tmp_path / "results.jsonl")
    runner = TrialRunner(writer)
    infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "provider 502"}
    ok = {"status": "pass", "passed": True}
    ran = []

    def execute(t):
        ran.append((t.model, t.task))
        return infra if t.model == "down" else ok

    gates = Gates(lambda t: (True, ""), execute, writer=writer)
    skipped = runner.run_trial(trial(0, model="down", task="a"), execute, gate=gates)
    assert skipped["status"] == "unavailable" and skipped["failures"] == ["provider 502"] * 2
    assert ("down", "a") not in ran  # the model's real trials were skipped
    again = runner.run_trial(trial(1, model="down", task="b"), execute, gate=gates)
    assert again["status"] == "unavailable"
    up = runner.run_trial(trial(2, model="up", task="a"), execute, gate=gates)
    assert up["passed"]
    rows, _ = read_results(tmp_path / "results.jsonl")
    assert [r["gate"] for r in rows if "gate" in r].count("canary") == 3  # 2 down + 1 up


def test_a_model_blocked_by_the_guardrail_is_infra_with_the_reason(tmp_path):
    executor, target, gateway = make(tmp_path, good_harness())

    def refuse(**kwargs):
        raise RuntimeError("gateway issue: model blocked by the key's guardrail: m")

    gateway.issue = refuse
    result = executor(trial())
    assert result["attribution"] == "infra" and "guardrail" in result["error"]
    assert "unit" not in target.events and "prepare" not in " ".join(target.events)


def test_a_declared_secret_found_on_the_target_is_infra_before_the_agent_runs(tmp_path):
    task = tmp_path / "tasks" / "keyed"
    shutil.copytree(CANARY, task)
    (task / "generate.py").write_text(
        "from pathlib import Path\n"
        "def generate(out, seed):\n"
        "    Path(out).mkdir(parents=True, exist_ok=True)\n"
        "    return {'answer': 'x', 'secrets': ['KEY-1234']}\n"
    )
    executor, target, _ = make(tmp_path, good_harness(), tasks={"canary": load_task(task)})
    searched: list[list[str]] = []
    hits: list[str] = []
    target.find_secrets = lambda ws, secrets: searched.append(secrets) or hits  # type: ignore[attr-defined]
    ok = executor(trial())
    assert searched == [["KEY-1234"]] and ok["passed"]
    hits.append("C:/trial/home/notes.txt")
    leaked = executor(trial(1))
    assert leaked["attribution"] == "infra" and "notes.txt" in leaked["error"]
    assert "KEY-1234" not in leaked["error"]  # locations only
    assert target.events.count("unit") == 1  # the second trial never reached the agent


def test_a_redact_only_secret_is_not_confinement_checked_but_is_still_redacted(tmp_path):
    """A database task's connection credentials (task 13.1) are legitimately reachable on the
    target -- declaring them under `redact` must not trip the confinement check `secrets` does,
    but must still end up in `trial_secrets` for report redaction."""
    task = tmp_path / "tasks" / "creds"
    shutil.copytree(CANARY, task)
    (task / "generate.py").write_text(
        "from pathlib import Path\n"
        "def generate(out, seed):\n"
        "    Path(out).mkdir(parents=True, exist_ok=True)\n"
        "    return {'answer': 'x', 'redact': ['hunter2']}\n"
    )
    executor, target, _ = make(tmp_path, good_harness(), tasks={"canary": load_task(task)})
    searched: list[list[str]] = []
    target.find_secrets = lambda ws, secrets: searched.append(secrets) or []  # type: ignore[attr-defined]
    result = executor(trial())
    assert searched == []  # never checked: a redact-only secret is expected to be reachable
    assert result["passed"] and result["trial_secrets"] == ["tok-0", "hunter2"]


def test_tools_the_model_was_offered_outside_the_configured_set_make_the_trial_infra(tmp_path):
    executor, _, gateway = make(tmp_path, good_harness())
    executor.adapter.visible_tools = lambda kind, tool_set: {"tool_search", "tool_call"}  # type: ignore[method-assign]
    row = dict(gateway.rows[0])
    gateway.rows = [row | {"tool_names": ["tool_call", "tool_search"]}]
    assert executor(trial())["passed"]  # exactly the expected tools were offered
    # a hidden agent (Qwen's memory agent) offered edit/shell tools with its own request
    gateway.rows = [row | {"tool_names": ["tool_search", "tool_call", "edit", "write_file"]}]
    result = executor(trial(1))
    assert result["attribution"] == "infra" and "['edit', 'write_file']" in result["error"]
    # requests without tool names (e.g. auxiliary ones) do not trip the check
    gateway.rows = [row | {"role": "auxiliary", "tool_names": ["edit"]}]
    assert executor(trial(2))["passed"]


def test_instruction_placeholders_are_filled_from_the_answer_key(tmp_path):
    from envelope_eval.tasks.execution import render_instruction

    key = {"params": {"a": 6, "b": "x y"}}
    assert render_instruction("run <<a>> then <<b>> then <<a>>", key) == "run 6 then x y then 6"
    assert render_instruction("no placeholders $((1*2)) <b>", key) == "no placeholders $((1*2)) <b>"
    with pytest.raises(KeyError, match="<<missing>>"):
        render_instruction("<<missing>>", key)
    task = tmp_path / "tasks" / "templ"
    shutil.copytree(CANARY, task)
    (task / "instruction.md").write_text("Print <<word>>.")
    (task / "generate.py").write_text(
        "def generate(out, seed):\n    return {'params': {'word': 'canary'}}\n"
    )
    executor, target, _ = make(tmp_path, good_harness(), tasks={"canary": load_task(task)})
    assert executor(trial())["passed"]
    assert target.units[0]["argv"] == ["fake-harness", "Print canary."]  # rendered per trial
    (task / "generate.py").write_text("def generate(out, seed):\n    return {'params': {}}\n")
    broken = executor(trial(1))
    assert broken["attribution"] == "infra" and "render instruction" in broken["error"]


def test_rate_limited_requests_are_not_model_turns_or_tokens(tmp_path):
    executor, _, gateway = make(tmp_path, good_harness())
    ok = {
        "role": "main",
        "end_reason": "complete",
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
    }
    limited = {
        "role": "main",
        "end_reason": "http_429",
        "usage": {"unknown": "no usage"},
        "retried": [429, 429],
    }
    gateway.rows = [limited, limited, limited, ok]
    result = executor(trial())
    assert result["turns"] == 1 and result["tokens"] == 110  # only the completed request
    assert result["failed_requests"] == 3 and result["absorbed_retries"] == 6
    assert result["main_model_turns"] == 1


def test_a_tripped_breaker_lets_one_attempt_through_after_the_cooldown():
    from envelope_eval.runner.lifecycle import CircuitBreaker

    breaker = CircuitBreaker(3)
    for _ in range(3):
        breaker.observe(succeeded=False, reason="429")
    breaker.stamp(100.0)
    assert breaker.tripped and not breaker.half_open(500.0, cooldown=900)  # too early
    assert breaker.half_open(1000.0, cooldown=900) and not breaker.tripped
    breaker.observe(succeeded=False, reason="429 again")  # a single failure trips it again
    assert breaker.tripped
    breaker.stamp(1001.0)
    assert breaker.half_open(2000.0, cooldown=900)
    breaker.observe(succeeded=True)
    assert not breaker.tripped and breaker.consecutive_failures == 0


def test_the_canary_gate_probes_a_failed_cell_again_after_the_cooldown(tmp_path):
    now = {"t": 0.0}
    healthy = {"up": False}
    probes = []

    def execute(t):
        probes.append(now["t"])
        infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "429"}
        return {"status": "pass", "passed": True} if healthy["up"] else infra

    gates = Gates(
        lambda t: (True, ""), execute, canary_trials=1, cooldown=600, clock=lambda: now["t"]
    )
    trial_ = trial(0)
    first = gates(trial_)
    assert first is not None and first.status == "unavailable" and len(probes) == 1
    now["t"] = 300
    assert gates(trial_) is first and len(probes) == 1  # cached inside the cooldown
    now["t"], healthy["up"] = 700, True
    assert gates(trial_) is None and len(probes) == 2  # probed again, and the cell is back
    now["t"] = 5000
    assert gates(trial_) is None and len(probes) == 2  # a good result is not re-probed


def test_parked_trials_are_returned_without_being_written(tmp_path):
    from envelope_eval.runner.lifecycle import CircuitBreaker

    writer = ResultWriter(tmp_path / "r.jsonl")
    runner = TrialRunner(writer, breakers={})
    infra = {"status": "infra", "passed": False, "attribution": "infra", "error": "429"}
    runner.max_retries = 0
    for i in range(3):
        runner.run_trial(trial(i), lambda t: infra)
    parked = runner.run_trial(trial(9), lambda t: infra, persist_unavailable=False)
    assert parked["status"] == "unavailable" and isinstance(
        runner.breaker(trial(9)), CircuitBreaker
    )
    rows, _ = read_results(tmp_path / "r.jsonl")
    assert len(rows) == 3 and all(
        r["status"] == "infra" for r in rows
    )  # the parked one is not there
    runner.write_unavailable(parked)
    assert read_results(tmp_path / "r.jsonl")[0][-1]["status"] == "unavailable"


def test_a_harness_without_a_tool_report_is_checked_against_the_gateway_instead(tmp_path):
    executor, _, gateway = make(tmp_path, good_harness(tools=()))  # the transcript lists no tools
    executor.adapter.exposed_tools = lambda trajectory: None  # type: ignore[method-assign]
    executor.adapter.reports_tools = False  # type: ignore[attr-defined]
    executor.adapter.visible_tools = lambda kind, tool_set: set(tool_set)  # type: ignore[method-assign]
    row = dict(gateway.rows[0])
    gateway.rows = [row | {"tool_names": sorted(TOOLS)}]
    assert executor(trial())[
        "passed"
    ]  # nothing to compare at start-up, and the offered tools match
    gateway.rows = [row | {"tool_names": [*sorted(TOOLS), "bash"]}]
    leaked = executor(trial(1))
    assert leaked["attribution"] == "infra" and "['bash']" in leaked["error"]


def test_a_provider_rejecting_the_models_tool_call_json_is_a_model_failure(tmp_path):
    executor, _, gateway = make(tmp_path, good_harness(answer="nope"))
    bad = {
        "role": "main",
        "timestamp": 100.0,
        "duration": 1.0,
        "end_reason": "stream_error",
        "error": {
            "code": 502,
            "message": "Upstream error: invalid arguments for tool_call: expected value",
        },
    }
    gateway.rows = [bad, bad, bad]
    result = executor(trial())
    assert result["attribution"] == "model"  # not indeterminate, not infra
    assert any(e["kind"] == "malformed_call" for e in result["timeline"])
    gateway.rows = [dict(bad, error={"code": 502, "message": "upstream unavailable"})] * 2
    assert executor(trial(1))["attribution"] == "infra"  # an ordinary provider failure still is


def test_the_trial_record_names_the_providers_that_served_it(tmp_path):
    executor, _, gateway = make(tmp_path, good_harness())
    gateway.rows = [
        dict(gateway.rows[0], provider="GMICloud"),
        dict(gateway.rows[0], provider="Together"),
    ]
    assert executor(trial())["providers"] == ["GMICloud", "Together"]
