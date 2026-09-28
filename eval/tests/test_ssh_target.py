"""SSH target: command lines, byte-preserving copies, tunnels, health, fingerprints, and the
runner pausing on a dropped tunnel. `ssh`/`scp` are replaced by fakes."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from envelope_eval.runner.lifecycle import JobAborted, TrialRunner
from envelope_eval.runner.planner import Trial
from envelope_eval.runner.results import ResultWriter
from envelope_eval.targets.base import FingerprintMismatch, verify_fingerprint
from envelope_eval.targets.ssh import (
    FINGERPRINT_SCRIPT,
    KIND,
    TAR,
    UNTAR,
    SSHTarget,
    TunnelError,
)


def done(returncode=0, out=b"", err=b""):
    return SimpleNamespace(returncode=returncode, stdout=out, stderr=err)


class FakeProc:
    def __init__(self, exit_code=None, stderr=b""):
        self.exit_code, self.returncode = exit_code, exit_code
        self.stderr = SimpleNamespace(read=lambda: stderr)
        self.killed = self.terminated = False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed, self.exit_code, self.returncode = True, -9, -9

    def terminate(self):
        self.terminated, self.exit_code, self.returncode = True, -15, -15


def target(run=None, popen=None, **kwargs) -> SSHTarget:
    t = SSHTarget("vm.lab", "envelope", **kwargs)
    t._run = run or (lambda argv, **kw: done())
    t._popen = popen or (lambda argv, **kw: FakeProc())
    return t


def test_commands_use_batch_mode_keepalive_and_pinned_options():
    seen: list[Any] = []
    t = target(lambda argv, **kw: seen.append(argv) or done(), key="/k", known_hosts="/kh")
    t.run(["cmd", "/c", "dir", "C:\\a b"])
    argv = seen[0]
    assert argv[0] == "ssh" and argv[-2] == "envelope@vm.lab"
    assert argv[-1] == subprocess.list2cmdline(["cmd", "/c", "dir", "C:\\a b"])
    for option in ("BatchMode=yes", "ServerAliveInterval=15", "IdentitiesOnly=yes"):
        assert option in argv
    assert "UserKnownHostsFile=/kh" in argv and "StrictHostKeyChecking=yes" in argv


def sample_tree(root: Path):
    (root / "logs").mkdir(parents=True)
    (root / "empty").mkdir()
    files = {
        "logs/app.log": "Привет, мир\r\n".encode("cp1251"),
        "u16.txt": "Привет".encode("utf-16-le"),
        "blob.bin": bytes(range(256)),
        "документы.txt": "тест".encode(),  # a non-ASCII file name
    }
    for name, data in files.items():
        (root / name).write_bytes(data)
    return files


def test_put_tree_sends_a_tar_stream_to_python_on_the_target(tmp_path):
    source = tmp_path / "fixtures"
    files = sample_tree(source)
    seen: dict[str, Any] = {}

    def run(argv, **kw):
        seen["command"], seen["input"] = argv[-1], kw["input"]
        return done()

    target(run).put(source, "C:\\eval\\trials\\t1\\workspace")
    assert "tarfile" in seen["command"] and seen["command"].endswith(
        "C:\\eval\\trials\\t1\\workspace"
    )
    with tarfile.open(fileobj=io.BytesIO(seen["input"])) as archive:
        names = set(archive.getnames())
    assert {"./logs/app.log", "./u16.txt", "./blob.bin", "./документы.txt", "./empty"} <= names
    assert files  # the contents are checked below by running the target-side code


def test_the_target_side_code_extracts_exact_bytes_and_names(tmp_path):
    source = tmp_path / "fixtures"
    files = sample_tree(source)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        archive.add(str(source), arcname=".")
    dest = tmp_path / "remote" / "workspace"  # neither it nor its parent exists yet
    proc = subprocess.run(
        [sys.executable, "-c", UNTAR, str(dest)],
        input=buffer.getvalue(),
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    for name, data in files.items():
        assert (dest / name).read_bytes() == data
    assert (dest / "empty").is_dir()
    # merging into an existing directory keeps what is there
    (dest / "keep.txt").write_text("x")
    again = subprocess.run(
        [sys.executable, "-c", UNTAR, str(dest)],
        input=buffer.getvalue(),
        capture_output=True,
        check=False,
    )
    assert again.returncode == 0 and (dest / "keep.txt").exists()


def test_the_download_code_produces_a_tar_and_kind_reports_missing(tmp_path):
    source = tmp_path / "trial"
    files = sample_tree(source)
    tarred = subprocess.run(
        [sys.executable, "-c", TAR, str(source)], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(tarred)) as archive:
        archive.extractall(tmp_path / "got", filter="data")
    for name, data in files.items():
        assert (tmp_path / "got" / name).read_bytes() == data
    kinds = {
        str(source): (0, b"dir"),
        str(source / "u16.txt"): (0, b"file"),
        str(source / "gone"): (3, b""),
    }
    for path, (code, word) in kinds.items():
        proc = subprocess.run([sys.executable, "-c", KIND, path], capture_output=True, check=False)
        assert proc.returncode == code and proc.stdout.strip() == word


def test_get_returns_trees_and_files_and_reports_missing(tmp_path):
    source = tmp_path / "trial"
    files = sample_tree(source)
    tarred = subprocess.run(
        [sys.executable, "-c", TAR, str(source)], capture_output=True, check=True
    ).stdout

    def run(argv, **kw):
        command = argv[-1]
        if "os.path.exists" in command:  # KIND
            if "missing" in command:
                return done(3)
            return done(0, b"dir\n" if "tree" in command else b"file\n")
        return done(0, tarred)  # TAR

    def scp(argv, **kw):
        Path(argv[-1], "audit.jsonl").write_bytes(b"\x00\x01")
        return done()

    t = target(lambda argv, **kw: scp(argv) if argv[0] == "scp" else run(argv, **kw))
    t.get("C:\\x\\tree", tmp_path / "after")
    for name, data in files.items():
        assert (tmp_path / "after" / name).read_bytes() == data
    t.get("C:\\x\\audit.jsonl", tmp_path / "audit.jsonl")
    assert (tmp_path / "audit.jsonl").read_bytes() == b"\x00\x01"
    with pytest.raises(FileNotFoundError):
        t.get("C:\\x\\missing", tmp_path / "nope")


def test_tunnel_is_loopback_only_and_fails_at_setup():
    with pytest.raises(ValueError, match="loopback"):
        target().open_reverse_tunnel(8787, 8787, bind="0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        target().open_reverse_tunnel(
            8787, 8787, bind="8.8.8.8"
        )  # a public address, not a bridge one
    started: list[Any] = []

    def popen(argv, **kw):
        started.append(argv)
        return FakeProc(
            exit_code=255, stderr=b"Error: remote port forwarding failed for listen port 8787"
        )

    with pytest.raises(TunnelError, match="remote port forwarding failed"):
        target(popen=popen).open_reverse_tunnel(8787, 8787, wait=2)
    argv = started[0]
    assert "ExitOnForwardFailure=yes" in argv and "-N" in argv
    assert argv[argv.index("-R") + 1] == "127.0.0.1:8787:127.0.0.1:8787"
    assert argv[-1] == "envelope@vm.lab"


def test_tunnel_can_bind_a_private_docker_bridge_address_not_just_loopback():
    seen: dict[str, Any] = {}

    def popen(argv, **kw):
        seen["argv"] = argv
        return FakeProc()

    def run(argv, **kw):
        return done(0, b"up\n")

    t = target(run, popen)
    tunnel = t.open_reverse_tunnel(18787, 18787, bind="172.18.0.1")  # a docker bridge gateway
    assert tunnel.bind == "172.18.0.1"
    assert seen["argv"][seen["argv"].index("-R") + 1] == "172.18.0.1:18787:127.0.0.1:18787"


def test_the_probe_checks_the_bind_address_actually_used_not_always_loopback():
    # A tunnel bound to a bridge address is only listening there; a probe that always checks
    # loopback (the earlier bug) would wrongly report success from an unrelated loopback listener.
    seen: list[str] = []

    def run(argv, **kw):
        seen.append(argv[-1])
        return done(0, b"up\n") if "'172.18.0.1'" in argv[-1] else done(1)

    t = target(run, popen=lambda argv, **kw: FakeProc())
    tunnel = t.open_reverse_tunnel(18787, 18787, bind="172.18.0.1", wait=2)
    assert tunnel.bind == "172.18.0.1" and any("'172.18.0.1'" in c for c in seen)
    assert t.tunnel_health() == []  # the health check also probes the real bind address


def test_tunnel_health_notices_a_dead_process_and_a_dead_listener():
    listening = {"up": True}

    def run(argv, **kw):
        return done(0, b"up\n") if listening["up"] else done(1)

    procs: list[FakeProc] = []

    def popen(argv, **kw):
        procs.append(FakeProc())
        return procs[-1]

    t = target(run, popen)
    t.open_reverse_tunnel(8787, 8787)
    assert t.tunnel_health() == []
    listening["up"] = False
    assert "nothing listening" in t.tunnel_health()[0]
    listening["up"] = True
    procs[0].exit_code, procs[0].returncode = 255, 255
    assert "ssh exited (255)" in t.tunnel_health()[0]
    t.restore_tunnels()  # reopens the dead tunnel
    assert len(procs) == 2 and t.tunnel_health() == []
    t.close_tunnels()
    assert procs[1].terminated


def test_fingerprint_is_measured_by_streaming_the_probe_and_compared_to_the_declaration():
    facts = {"oemcp": "437", "acp": "1252", "versions": {"qwen": "0.24.5"}}
    seen: dict[str, Any] = {}

    def run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw["input"]
        return done(0, json.dumps(facts).encode())

    t = target(run, tools=("qwen",))
    measured = t.fingerprint()
    assert seen["input"] == FINGERPRINT_SCRIPT.read_bytes()
    assert seen["argv"][-1] == subprocess.list2cmdline(["python", "-", "--tool", "qwen"])
    assert measured.facts == facts and len(measured.digest) == 64
    ok = {"oemcp": 437, "versions": {"qwen": "0.24.5"}}
    assert verify_fingerprint("vm", t, ok).facts == facts
    with pytest.raises(FingerprintMismatch) as excinfo:
        verify_fingerprint("vm", t, {"oemcp": 866, "versions": {"qwen": "0.25.0"}})
    text = str(excinfo.value)
    assert "oemcp: declared 866, measured '437'" in text and "versions.qwen" in text


def trial(i=0):
    return Trial(i, "windows", "canary", "default", i, "vm", "qwen", "m")


PASS = {"status": "pass", "passed": True}


def test_job_pauses_until_the_tunnel_is_restored_before_a_trial(tmp_path):
    state = {"down": True, "now": 0.0, "restores": 0, "ran": 0}

    def restore():
        state["restores"] += 1
        if state["restores"] == 3:
            state["down"] = False

    def sleep(seconds):
        state["now"] += seconds

    runner = TrialRunner(
        ResultWriter(tmp_path / "r.jsonl"),
        clock=lambda: state["now"],
        sleep=sleep,
        health=lambda: ["tunnel 8787: ssh exited"] if state["down"] else [],
        restore=restore,
        poll=5,
    )

    def execute(t):
        state["ran"] += 1
        return PASS

    record = runner.run_trial(trial(), execute)
    assert record["passed"] and state["ran"] == 1 and state["restores"] == 3
    assert runner.paused == 15


def test_a_tunnel_that_drops_during_a_trial_makes_it_infra_and_the_retry_waits(tmp_path):
    state = {"down": False, "now": 0.0, "runs": 0}

    def execute(t):
        state["runs"] += 1
        if state["runs"] == 1:
            state["down"] = True  # the tunnel drops while the agent works
        return PASS

    def restore():
        state["down"] = False

    runner = TrialRunner(
        ResultWriter(tmp_path / "r.jsonl"),
        clock=lambda: state["now"],
        sleep=lambda s: state.update(now=state["now"] + s),
        health=lambda: ["tunnel down"] if state["down"] else [],
        restore=restore,
    )
    record = runner.run_trial(trial(), execute)
    assert state["runs"] == 2 and record["passed"] and record["retries"] == 1
    assert "target unhealthy during the trial" in record["retry_reasons"][0]
    assert runner.paused >= 0


def test_the_job_aborts_when_the_target_never_recovers(tmp_path):
    now = {"t": 0.0}
    runner = TrialRunner(
        ResultWriter(tmp_path / "r.jsonl"),
        clock=lambda: now["t"],
        sleep=lambda s: now.update(t=now["t"] + s),
        health=lambda: ["tunnel down"],
        pause_timeout=60,
        poll=10,
    )
    ran: list[int] = []
    with pytest.raises(JobAborted, match="tunnel down"):
        runner.run_trial(trial(), lambda t: ran.append(1) or PASS)
    assert ran == []  # no trial started while the target was down
