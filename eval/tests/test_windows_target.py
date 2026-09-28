"""WindowsTrialTarget against a fake SSH target: what runs on the host, and where."""

import json
from pathlib import Path
from typing import Any

import pytest

from envelope_eval.targets.ssh import SSHTarget
from envelope_eval.targets.windows import WindowsTrialTarget


class FakeSSH(SSHTarget):
    def __init__(self):
        super().__init__("vm", "envelope")
        self.shell: list[str] = []
        self.puts: list[tuple[str, str, bytes | None]] = []
        self.runs: list[tuple[list[str], bytes | None]] = []
        self.shell_result: tuple[int, bytes, bytes] = (0, b"", b"")
        self.launch_result: tuple[int, bytes, bytes] = (0, b"", b"")
        self.run_result: tuple[int, bytes, bytes] = (0, b"[]", b"")

    def run_shell(self, command, timeout=None):
        self.shell.append(command)
        return self.launch_result if "launch.py" in command else self.shell_result

    def put(self, source, destination):
        data = source.read_bytes() if source.is_file() else None
        self.puts.append((source.name, destination, data))

    def run(self, argv, timeout=None, input=None):
        self.runs.append((argv, input))
        return self.run_result


def target():
    ssh = FakeSSH()
    return WindowsTrialTarget(ssh), ssh


def test_setup_creates_directories_as_the_user_and_installs_the_launcher(tmp_path):
    t, ssh = target()
    pyz = tmp_path / "envelope.pyz"
    pyz.write_bytes(b"PK")
    t.setup(pyz)
    assert 'if not exist "C:\\eval\\trials" mkdir "C:\\eval\\trials"' in ssh.shell[0]
    assert [dest for _, dest, _ in ssh.puts] == [
        "C:\\eval\\launcher\\launch.py",
        "C:\\eval\\launcher\\oracle.py",
        "C:\\eval\\envelope.pyz",
    ]


def test_each_trial_gets_its_own_directories_and_environment():
    t, ssh = target()
    a, b = t.prepare("job-t00001"), t.prepare("job-t00002")
    base = "C:\\eval\\trials\\job-t00001"
    assert ssh.shell[0] == f'if exist "{base}" rmdir /s /q "{base}"'  # nothing stale survives
    assert a.workspace == f"{base}\\workspace" and a.home == f"{base}\\home"
    assert a.env["HOME"] == a.env["USERPROFILE"] == a.home
    assert a.env["APPDATA"].startswith(a.home) and a.env["LOCALAPPDATA"].startswith(a.home)
    assert a.env["GIT_CONFIG_GLOBAL"] == f"{a.home}\\.gitconfig"
    assert a.env["ENVELOPE_AUDIT_LOG"] == a.audit_log == f"{base}\\state\\audit.jsonl"
    assert a.env["ENVELOPE_SPOOL_DIR"] == a.spool
    assert "GIT_CONFIG_NOSYSTEM" not in a.env  # the system gitconfig is part of the environment
    for key in ("workspace", "home", "state", "audit_log", "spool"):
        assert getattr(a, key) != getattr(b, key)  # nothing is shared between trials
    assert set(a.env.values()).isdisjoint(b.env.values())


def test_run_unit_writes_a_spec_and_reports_the_launchers_result():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    result = {
        "exit_code": 0,
        "timed_out": False,
        "leftovers": [{"pid": 4242, "name": "sleep.exe"}],
        "at_timeout": [],
        "emptied": True,
        "duration": 1.5,
        "error": None,
    }
    ssh.launch_result = (0, (json.dumps(result) + "\n").encode(), b"")
    unit = t.run_unit(
        ["qwen", "-p", "hi"],
        env=ws.env,
        cwd=ws.workspace,
        timeout=90,
        stdout_to=f"{ws.state}\\transcript.jsonl",
    )
    assert unit.exit_code == 0 and not unit.timed_out and unit.leftovers == ("4242 sleep.exe",)
    _, dest, data = next(p for p in ssh.puts if p[0] == "spec.json")
    assert dest == f"{ws.state}\\spec.json"
    spec: dict[str, Any] = json.loads(data or b"")
    assert spec["argv"] == ["qwen", "-p", "hi"] and spec["timeout"] == 90
    assert spec["stdout"].endswith("transcript.jsonl") and spec["env"]["HOME"] == ws.home
    launch = ssh.shell[-1]
    assert launch.startswith('"python" "C:\\eval\\launcher\\launch.py"') and "spec.json" in launch
    assert any('mkdir "' + ws.env["APPDATA"] in c for c in ssh.shell)  # programs expect these dirs


def test_run_unit_failures_are_errors_not_results():
    t, ssh = target()
    ws = t.prepare("job-t00001")

    def run():
        return t.run_unit([], env=ws.env, cwd=ws.workspace, timeout=5, stdout_to=ws.state + "\\o")

    ssh.launch_result = (1, b"", b"python: can't open file")
    with pytest.raises(RuntimeError, match="launcher failed"):
        run()
    bad = {"exit_code": None, "timed_out": False, "leftovers": [], "error": "not found"}
    ssh.launch_result = (0, json.dumps(bad).encode(), b"")
    with pytest.raises(RuntimeError, match="could not start the harness: not found"):
        run()


def test_collect_runs_in_the_workspace_in_utf8_mode(tmp_path):
    t, ssh = target()
    ws = t.prepare("job-t00001")
    script = tmp_path / "collect.py"
    script.write_text("print(1)")
    t.collect(script, ws, 60)
    assert ssh.puts[-1][1] == f"{ws.state}\\collect.py"
    assert ssh.shell[-1] == (
        f'set "ENVELOPE_AUDIT_LOG={ws.audit_log}"&& set "ENVELOPE_SPOOL_DIR={ws.spool}"&& '
        f'cd /d "{ws.workspace}" && "python" -X utf8 "{ws.state}\\collect.py"'
    )


def test_confinement_search_sends_the_needles_on_stdin_not_the_command_line():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    ssh.run_result = (0, json.dumps([f"{ws.home}\\notes.txt"]).encode(), b"")
    hits = t.find_secrets(ws, ["answer-42", "seed-7"])
    assert hits == [f"{ws.home}\\notes.txt"]
    argv, script = ssh.runs[0]
    assert argv == ["python", "-"]
    text = (script or b"").decode()
    assert "answer-42" in text and "seed-7" in text
    assert repr("C:\\eval\\trials\\job-t00001") in text  # searches the whole trial directory


def test_cleanup_removes_the_whole_trial_directory():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    t.cleanup(ws)
    base = "C:\\eval\\trials\\job-t00001"
    assert ssh.shell[-1] == f'if exist "{base}" rmdir /s /q "{base}"'
    ssh.shell_result = (5, b"", b"Access is denied")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        t.cleanup(ws)
    assert Path(__file__).exists()
