"""LinuxTrialTarget against a fake SSH target: what runs on the host, and the container-prefixed
workspace paths that let one target instance serve concurrent trials safely."""

import json

import pytest

from envelope_eval.targets.linux import LinuxTrialTarget, _split
from envelope_eval.targets.ssh import SSHTarget


class FakeSSH(SSHTarget):
    def __init__(self):
        super().__init__("linux-host", "envelope", windows=False)
        self.shell: list[str] = []
        self.runs: list[tuple[list[str], bytes | None]] = []
        self.puts: list[tuple[str, str, bytes | None]] = []
        self.gets: list[tuple[str, str]] = []
        self.shell_result: tuple[int, bytes, bytes] = (0, b"", b"")
        self.test_f_result: tuple[int, bytes, bytes] = (1, b"", b"")  # "not found" by default
        self.launch_result: tuple[int, bytes, bytes] = (0, b"", b"")
        self.run_result: tuple[int, bytes, bytes] = (0, b"[]", b"")

    def run_shell(self, command, timeout=None):
        self.shell.append(command)
        if command.startswith("test -f"):
            return self.test_f_result
        return self.shell_result

    def run(self, argv, timeout=None, input=None):
        self.runs.append((argv, input))
        if argv[:2] == ["docker", "exec"] and "launch.py" in argv[-2]:
            return self.launch_result
        return self.run_result

    def put(self, source, destination):
        data = source.read_bytes() if source.is_file() else None
        self.puts.append((str(source), destination, data))

    def get(self, source, destination):
        self.gets.append((source, str(destination)))


def target():
    ssh = FakeSSH()
    return LinuxTrialTarget(ssh, "envelope-eval-linux:2026-09-28"), ssh


def test_split_parses_the_container_prefixed_pseudo_path():
    assert _split("envelope-eval-t1:/home/trial/workspace") == (
        "envelope-eval-t1",
        "/home/trial/workspace",
    )
    with pytest.raises(ValueError, match="not a container-prefixed path"):
        _split("/plain/path")


def test_real_path_strips_the_container_prefix_for_the_adapters_own_command_line():
    t, _ssh = target()
    assert t.real_path("envelope-eval-t1:/home/trial/workspace") == "/home/trial/workspace"


def test_setup_creates_the_internal_network_and_stages_the_launcher():
    t, ssh = target()
    t.setup()
    assert "docker network create --internal envelope-eval" in ssh.shell[0]
    assert [d for _, d, _ in ssh.puts] == [
        "/home/envelope/eval-launcher/launch.py",
        "/home/envelope/eval-launcher/oracle.py",
    ]


def test_setup_can_also_stage_envelope_pyz_and_prepare_copies_it_in(tmp_path):
    t, ssh = target()
    pyz = tmp_path / "envelope.pyz"
    pyz.write_bytes(b"PK")
    t.setup(pyz)
    assert [d for _, d, _ in ssh.puts][-1] == "/home/envelope/eval-launcher/envelope.pyz"
    ssh.test_f_result = (0, b"", b"")  # `test -f .../envelope.pyz` succeeds
    t.prepare("job-t00001")
    assert any(
        "docker cp /home/envelope/eval-launcher/envelope.pyz "
        "envelope-eval-job-t00001:/home/trial/envelope.pyz" == c
        for c in ssh.shell
    )


def test_setup_stages_both_builds_for_an_interleaved_regression_job(tmp_path):
    t, ssh = target()
    current = tmp_path / "current.pyz"
    baseline = tmp_path / "baseline.pyz"
    current.write_bytes(b"P3a")
    baseline.write_bytes(b"P2")
    t.setup(current, variants={"p2": baseline})
    ssh.test_f_result = (0, b"", b"")
    t.prepare("regression-t00001")
    assert any(dest.endswith("/envelope-p2.pyz") and data == b"P2" for _, dest, data in ssh.puts)
    assert any(
        command == "docker cp /home/envelope/eval-launcher/envelope-p2.pyz "
        "envelope-eval-regression-t00001:/home/trial/envelope-p2.pyz"
        for command in ssh.shell
    )


def test_prepare_skips_envelope_pyz_when_setup_never_staged_one():
    t, ssh = target()
    t.setup()  # no envelope_pyz argument: test_f_result stays "not found" by default
    t.prepare("job-t00002")
    assert not any(c.startswith("docker cp") and "envelope.pyz" in c for c in ssh.shell)


def test_each_trial_gets_its_own_container_and_workspace_paths_carry_it():
    t, ssh = target()
    a = t.prepare("job-t00001")
    name = "envelope-eval-job-t00001"
    assert any(f"docker rm -f {name}" == c for c in ssh.shell[:1])
    assert any(
        "docker run -d --name " + name in c and "--network envelope-eval" in c for c in ssh.shell
    )
    assert a.workspace == f"{name}:/home/trial/workspace" and a.home == f"{name}:/home/trial/home"
    assert a.env["HOME"] == "/home/trial/home"  # a real in-container path, not container-prefixed
    assert a.env["ENVELOPE_AUDIT_LOG"] == "/home/trial/state/audit.jsonl" == _split(a.audit_log)[1]
    b = t.prepare("job-t00002")
    assert a.workspace != b.workspace and _split(a.workspace)[0] != _split(b.workspace)[0]


def test_upload_stages_on_the_host_then_docker_cps_into_the_named_container(tmp_path):
    t, ssh = target()
    ws = t.prepare("job-t00001")
    local = tmp_path / "fixtures"
    local.mkdir()
    t.upload(local, f"{ws.workspace}/data")
    staged_local, staged_dest, _ = ssh.puts[-1]
    assert staged_local == str(local)
    cp = next(c for c in ssh.shell if c.startswith("docker cp") and staged_dest in c)
    # a trailing "/." merges the directory's contents into the destination instead of nesting a
    # copy of the source directory underneath it (docker cp's own gotcha, mirroring scp's on Windows)
    assert cp == f"docker cp {staged_dest}/. envelope-eval-job-t00001:/home/trial/workspace/data"


def test_uploading_a_single_file_does_not_get_the_directory_merge_suffix(tmp_path):
    t, ssh = target()
    ws = t.prepare("job-t00001")
    local = tmp_path / "collect.py"
    local.write_text("print(1)")
    t.upload(local, f"{ws.state}/collect.py")
    cp = next(c for c in ssh.shell if c.startswith("docker cp") and "collect.py" in c)
    assert not cp.split()[1].endswith("/.")


@pytest.mark.parametrize(
    "message",
    [
        "Error: No such container:path: ...",
        "Error response from daemon: Could not find the file /a/b in container envelope-eval-x\n",
    ],
)
def test_download_reports_missing_for_either_docker_cp_error_wording(tmp_path, message):
    t, ssh = target()
    ws = t.prepare("job-t00001")
    ssh.shell_result = (1, b"", message.encode())
    with pytest.raises(FileNotFoundError):
        t.download(f"{ws.workspace}/gone", tmp_path / "x")


def test_download_otherwise_pulls_through_staging(tmp_path):
    t, ssh = target()
    ws = t.prepare("job-t00001")
    ssh.shell_result = (0, b"", b"")
    t.download(f"{ws.workspace}/data", tmp_path / "after")
    assert ssh.gets[-1][1] == str(tmp_path / "after")


def test_run_unit_splits_the_container_out_of_cwd_and_stdout_and_reports_the_launcher_result():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    result = {
        "exit_code": 0,
        "timed_out": False,
        "leftovers": [{"pid": 42, "name": "sleep"}],
        "at_timeout": [],
        "emptied": True,
        "duration": 1.0,
        "error": None,
    }
    ssh.launch_result = (0, (json.dumps(result) + "\n").encode(), b"")
    unit = t.run_unit(
        ["qwen", "-p", "hi"],
        env={"OPENAI_MODEL": "m"},
        cwd=ws.workspace,
        timeout=90,
        stdout_to=f"{ws.state}/transcript.jsonl",
        stdin_from=f"{ws.state}/instruction.txt",
    )
    assert unit.exit_code == 0 and unit.leftovers == ("42 sleep",)
    argv, _ = next(
        r for r in ssh.runs if r[0][:2] == ["docker", "exec"] and "launch.py" in r[0][-2]
    )
    assert argv == [
        "docker", "exec", "envelope-eval-job-t00001", "python3",
        "/home/trial/launch.py", "/home/trial/state/spec.json",
    ]  # fmt: skip
    spec_data = next(data for _, dest, data in ssh.puts if "spec.json" in dest)
    assert spec_data is not None
    spec = json.loads(spec_data)
    assert (
        spec["cwd"] == "/home/trial/workspace"
        and spec["stdout"] == "/home/trial/state/transcript.jsonl"
    )
    assert spec["stdin"] == "/home/trial/state/instruction.txt"
    assert spec["env"] == {"OPENAI_MODEL": "m"}  # no container prefix leaks into the harness env


def test_run_unit_raises_on_a_launcher_failure_not_a_harness_failure():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    ssh.launch_result = (1, b"", b"docker: Error response from daemon: OOM")
    with pytest.raises(RuntimeError, match="launcher failed"):
        t.run_unit([], env={}, cwd=ws.workspace, timeout=5, stdout_to=f"{ws.state}/t")


def test_collect_sets_cwd_and_envelope_env_flags_and_uploads_the_script(tmp_path):
    t, ssh = target()
    ws = t.prepare("job-t00001")
    script = tmp_path / "collect.py"
    script.write_text("print(1)")
    t.collect(script, ws, 60)
    argv, _ = ssh.runs[-1]
    assert argv[:3] == ["docker", "exec", "-w"] and "/home/trial/workspace" in argv
    assert "-e" in argv and any(a.startswith("ENVELOPE_AUDIT_LOG=") for a in argv)
    assert argv[-3:] == ["python3", "-X", "utf8"] or argv[-1].endswith("collect.py")


def test_cleanup_lists_processes_before_removing_the_container():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    ssh.shell.clear()  # prepare() itself already issued a preemptive "docker rm -f"
    t.cleanup(ws)
    top_index = next(i for i, c in enumerate(ssh.shell) if c.startswith("docker top"))
    rm_index = next(
        i for i, c in enumerate(ssh.shell) if c == "docker rm -f envelope-eval-job-t00001"
    )
    assert top_index < rm_index


def test_confinement_search_embeds_the_needles_in_the_script_not_a_file_or_command_line():
    t, ssh = target()
    ws = t.prepare("job-t00001")
    ssh.run_result = (0, json.dumps(["/home/trial/home/notes.txt"]).encode(), b"")
    hits = t.find_secrets(ws, ["answer-42", "seed-7"])
    assert hits == ["/home/trial/home/notes.txt"]
    argv, script = next(r for r in ssh.runs if r[0][:3] == ["docker", "exec", "-i"])
    assert argv[3] == "envelope-eval-job-t00001" and argv[-2:] == ["python3", "-"]
    text = (script or b"").decode()
    assert "answer-42" in text and "seed-7" in text
    assert not any("answer-42" in str(a) for r in ssh.runs for a in r[0])  # never on a command line


def test_fingerprint_reports_the_image_digest_and_docker_version():
    t, ssh = target()
    ssh.shell_result = (0, b"sha256:abc123\n", b"")
    fp = t.fingerprint()
    assert fp.facts["image_digest"] == "sha256:abc123"


def test_network_gateway_reports_the_trial_networks_own_bridge_address():
    t, ssh = target()
    ssh.shell_result = (0, b"172.18.0.1\n", b"")
    assert t.network_gateway() == "172.18.0.1"
    assert "network inspect" in ssh.shell[-1] or "docker network inspect" in ssh.shell[-1]


def test_network_gateway_raises_when_the_network_has_no_gateway():
    t, ssh = target()
    ssh.shell_result = (0, b"\n", b"")
    with pytest.raises(RuntimeError, match="no gateway address"):
        t.network_gateway()
