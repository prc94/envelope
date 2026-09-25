"""End-to-end scenarios over the real stdio protocol (POSIX hosts)."""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.integration.client import StdioClient, body_lines, text
from tests.integration.conftest import linux_only, pgrep, posix_only

pytestmark = [pytest.mark.integration, posix_only]


def unique_sleep() -> str:
    return f"{random.randint(300, 900)}.{random.randint(1000, 9999)}"


def gone(marker: str, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not pgrep(marker):
            return True
        time.sleep(0.1)
    return False


# --- basics and rendering ---------------------------------------------------------


def test_echo_header_and_body(client: StdioClient) -> None:
    result = client.run("echo hi")
    lines = text(result).splitlines()
    assert lines[0].startswith("exit 0 | ") and lines[0].endswith(f"| bash | cwd {client.workdir}")
    assert lines[1:] == ["hi"]
    assert "isError" not in result
    structured = result["structuredContent"]
    assert structured["exit_code"] == 0 and structured["shell"] == "bash"
    assert "hi" not in json.dumps(structured)
    assert len(json.dumps(structured)) < 1000


def test_failing_command_is_not_tool_error(client: StdioClient) -> None:
    result = client.run("echo some output; exit 3")
    assert "isError" not in result
    assert text(result).startswith("exit 3 | ")
    assert "note:" not in text(result)  # generic failure -> no hints


def test_empty_output(client: StdioClient) -> None:
    assert body_lines(client.run("true")) == ["(no output)"]


def test_unknown_shell_lists_available(client: StdioClient) -> None:
    result = client.run("echo x", shell="tcsh")
    assert result["isError"] is True
    assert "Available shells: bash" in text(result)


def test_interleaved_streams(client: StdioClient) -> None:
    result = client.run("echo a; sleep 0.2; echo b >&2; sleep 0.2; echo c")
    assert body_lines(result) == ["a", "[stderr] b", "c"]


def test_state_does_not_persist(client: StdioClient) -> None:
    client.run("cd /tmp && export X=1")
    assert body_lines(client.run('pwd; echo "[$X]"')) == [str(client.workdir), "[]"]


def test_missing_cwd(client: StdioClient) -> None:
    result = client.run("pwd", cwd="/does/not/exist")
    assert result["isError"] is True and "does not exist: /does/not/exist" in text(result)


def test_stdin_closed_by_default(client: StdioClient) -> None:
    started = time.monotonic()
    result = client.run('read -p "Continue? " x; echo "got:$x"')
    assert time.monotonic() - started < 10
    assert body_lines(result) == ["got:"]


def test_stdin_provided(client: StdioClient) -> None:
    assert body_lines(client.run("wc -l", stdin="a\nb\nc\n"))[0].strip() == "3"


def test_prompt_hint_when_stdin_closed(client: StdioClient) -> None:
    result = client.run("printf 'Overwrite existing file? [y/N] '; read x || true")
    assert "stdin is closed" in text(result)


def test_large_bash_script(client: StdioClient) -> None:
    lines = [f"v{i}=$(( {i} * 2 ))  # {'x' * 60}" for i in range(1500)]
    script = "\n".join(lines) + '\necho "last=$v1499"\nexit 4\n'
    assert len(script) > 100_000
    result = client.run(script)
    assert text(result).startswith("exit 4 | ")
    assert body_lines(result) == ["last=2998"]


def test_sigkill_hint(client: StdioClient) -> None:
    result = client.run("kill -9 $$")
    assert text(result).startswith("signal 9 (SIGKILL)") or "exit 137" in text(result)
    assert "possibly by the out-of-memory killer" in text(result)


def test_command_not_found_hint(client: StdioClient) -> None:
    result = client.run("definitely-not-a-command-xyz --help")
    assert text(result).startswith("exit 127")
    assert "`definitely-not-a-command-xyz` was not found in bash" in text(result)


def test_binary_output(client: StdioClient) -> None:
    png = client.workdir / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20)
    result = client.run("cat image.png")
    assert "[binary output on stdout: 5,128 bytes (PNG)]" in text(result)
    assert result["structuredContent"]["streams"]["stdout"]["binary"] == "PNG"


# --- environment -----------------------------------------------------------------------


def test_env_defaults_scrub_and_override(tmp_path: Path) -> None:
    c = StdioClient(
        tmp_path, config='default_shell = "bash"\n', env={"OPENROUTER_API_KEY": "sk-secret"}
    )
    try:
        c.initialize()
        assert body_lines(c.run('echo "[$OPENROUTER_API_KEY]"')) == ["[]"]
        assert body_lines(c.run('echo "$TERM $PAGER $GIT_PAGER $NO_COLOR"')) == ["dumb cat cat 1"]
        assert body_lines(c.run('echo "$TERM"', env={"TERM": "xterm"})) == ["xterm"]
        assert body_lines(
            c.run('echo "$OPENROUTER_API_KEY"', env={"OPENROUTER_API_KEY": "given"})
        ) == ["given"]
    finally:
        c.close()
    records = [r for r in c.audit() if r.get("event") == "tool_call"]
    assert "OPENROUTER_API_KEY" in records[0].get("env_removed", [])
    assert "sk-secret" not in c.audit_path.read_text()


def test_git_log_does_not_page(client: StdioClient) -> None:
    repo = client.workdir / "repo"
    script = (
        f"git init -q {repo} && cd {repo} && git config user.email t@t && git config user.name t && "
        'for i in $(seq 1 60); do git commit -q --allow-empty -m "commit $i"; done && git log --oneline | wc -l'
    )
    setup = client.run(script, timeout_s=60)
    assert body_lines(setup)[-1].strip() == "60"
    result = client.run("git log", cwd=str(repo), timeout_s=20)
    assert text(result).startswith("exit 0")


# --- coercion ------------------------------------------------------------------------------


def test_coercions(client: StdioClient) -> None:
    result = client.tool("run", {"command": "echo $FOO", "timeout_s": "30", "env": '{"FOO":"1"}'})
    assert body_lines(result) == ["1"]
    record = [r for r in client.audit() if r.get("event") == "tool_call"][-1]
    coerced = {c["param"] for c in record["coercions"]}
    assert {"timeout_s", "env"} <= coerced
    assert record["request"]["timeout_s"] == 30
    lines = client.tool("run", {"command": "echo $A$B", "env": "A=x\nB=y"})
    assert body_lines(lines) == ["xy"]
    bad = client.tool("run", {"command": "true", "timeout_s": "soon"})
    assert bad["isError"] is True and "`timeout_s` must be a number of seconds" in text(bad)


# --- timeouts, trees, concurrency ----------------------------------------------------------


def test_timeout_kills_tree(client: StdioClient) -> None:
    marker = unique_sleep()
    started = time.monotonic()
    result = client.run(f"echo started; sleep {marker}", timeout_s=2)
    elapsed = time.monotonic() - started
    assert 1.5 < elapsed < 8
    assert text(result).startswith("timed out after 2s")
    assert "started" in body_lines(result)
    assert "all its child processes were terminated" in text(result)
    assert result["structuredContent"]["timed_out"] is True
    assert gone(f"sleep {marker}")


def test_background_jobs_killed_on_timeout(client: StdioClient) -> None:
    a, b = unique_sleep(), unique_sleep()
    client.run(f"sleep {a} & sleep {b}", timeout_s=1)
    assert gone(f"sleep {a}") and gone(f"sleep {b}")


def test_detached_background_job_does_not_delay_completion(client: StdioClient) -> None:
    # A backgrounded job that redirects its own output away must not hold the
    # launch shim's stdout/stderr open: the call should return as soon as the
    # foreground command finishes, not sit until timeout_s waiting for a
    # process the caller never asked to wait for.
    marker = unique_sleep()
    started = time.monotonic()
    result = client.run(f"sleep {marker} >/dev/null 2>&1 & echo done", timeout_s=30)
    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert text(result).startswith("exit 0")
    assert body_lines(result) == ["done"]
    assert result["structuredContent"]["timed_out"] is False
    assert gone(f"sleep {marker}")


def test_timeout_clamped(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\nmax_timeout_s = 5\n')
    try:
        c.initialize()
        result = c.run("echo ok", timeout_s=999999)
        assert "clamped to 5s" in text(result)
        record = [r for r in c.audit() if r.get("event") == "tool_call"][-1]
        assert record["request"]["timeout_s"] == 5
    finally:
        c.close()


def test_default_timeout_from_flag_overrides_file(tmp_path: Path) -> None:
    c = StdioClient(
        tmp_path,
        config='default_shell = "bash"\ndefault_timeout_s = 60\n',
        args=("--default-timeout", "30"),
    )
    try:
        c.initialize()
        c.run("true")
        record = [r for r in c.audit() if r.get("event") == "tool_call"][-1]
        assert record["request"]["timeout_s"] == 30
    finally:
        c.close()


def test_concurrency_limit(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    log = tmp_path / "work" / "log"
    try:
        c.initialize()
        ids = [
            c.request(
                "tools/call",
                {
                    "name": "run",
                    "arguments": {
                        "command": f"echo start $(date +%s.%N) >> {log}; sleep 1.5; echo end $(date +%s.%N) >> {log}"
                    },
                },
            )
            for _ in range(10)
        ]
        results = [c.response(rid, 60) for rid in ids]
    finally:
        c.close()
    assert all(r["result"]["content"][0]["text"].startswith("exit 0") for r in results)
    events = sorted(
        (float(t), kind) for kind, t in (line.split() for line in log.read_text().splitlines())
    )
    running = peak = 0
    for _, kind in events:
        running += 1 if kind == "start" else -1
        peak = max(peak, running)
    assert peak == 8


def test_spawn_failure(tmp_path: Path) -> None:
    fake = tmp_path / "noexec-shell"
    fake.write_text("#!/bin/sh\necho hi\n")
    fake.chmod(0o644)
    config = (
        f'default_shell = "bash"\n[shells.broken]\npath = "{fake}"\nargs = ["-c", "{{script}}"]\n'
    )
    c = StdioClient(tmp_path, config=config)
    try:
        c.initialize()
        result = c.run("echo x", shell="broken")
    finally:
        c.close()
    assert result["isError"] is True
    assert f"could not start shell 'broken' at {fake}" in text(result)
    assert "Permission denied" in text(result)


def test_custom_shell(tmp_path: Path) -> None:
    config = (
        'default_shell = "bash"\n'
        '[shells.mysh]\npath = "/bin/sh"\nargs = ["-c", "{script}"]\n'
        '[shells.fsh]\npath = "/bin/sh"\nargs = ["{file}"]\nextension = ".sh"\n'
    )
    c = StdioClient(tmp_path, config=config)
    try:
        c.initialize()
        tools = c.call("tools/list")["result"]["tools"]
        assert "mysh" in tools[0]["description"]
        assert body_lines(c.run("echo custom-$((1+2))", shell="mysh")) == ["custom-3"]
        assert body_lines(c.run("echo from-file", shell="fsh")) == ["from-file"]
    finally:
        c.close()


# --- policy ---------------------------------------------------------------------------------


def test_policy_cwd_roots_and_default_allow(tmp_path: Path) -> None:
    allowed = tmp_path / "work"
    c = StdioClient(tmp_path, config=f'default_shell = "bash"\nallowed_cwd_roots = ["{allowed}"]\n')
    try:
        c.initialize()
        denied = c.run("ls", cwd="/")
        assert denied["isError"] is True
        assert "denied by policy (rule allowed_cwd_roots)" in text(denied)
        assert str(allowed) in text(denied)
        (allowed / "build").mkdir()
        ok = c.run("rm -rf ./build && echo removed")
        assert body_lines(ok) == ["removed"] and not (allowed / "build").exists()
    finally:
        c.close()
    decisions = [r for r in c.audit() if r.get("event") == "decision"]
    assert decisions[0]["decision"]["verdict"] == "deny"
    assert decisions[1]["decision"]["verdict"] == "allow"


def test_policy_denied_shell_and_audit_only(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\nallowed_shells = ["sh"]\n')
    try:
        c.initialize()
        denied = c.run("echo x", shell="bash")
        assert denied["isError"] is True
        assert "denied by policy (rule allowed_shells)" in text(denied)
        assert "allowed_shells in the envelope configuration" in text(denied)
    finally:
        c.close()
    c = StdioClient(
        tmp_path / "b",
        config='default_shell = "bash"\nallowed_shells = ["sh"]\naudit_only = true\n',
    )
    try:
        c.initialize()
        result = c.run("echo ran")
        assert body_lines(result)[0] == "ran"
        assert "audit-only mode: this request would have been denied" in text(result)
    finally:
        c.close()
    decision = next(r for r in c.audit() if r.get("event") == "decision")["decision"]
    assert decision["verdict"] == "deny" and decision["audit_only"] is True


def test_decision_recorded_before_start(client: StdioClient) -> None:
    client.run("echo hi")
    events = [r["event"] for r in client.audit()]
    assert events == ["decision", "tool_call"]
    record = client.audit()[-1]
    assert record["decision"]["verdict"] == "allow"
    assert record["outcome"]["exit_code"] == 0 and "duration_ms" in record["outcome"]
    assert record["arguments"]["command"] == "echo hi"


# --- output tool through the protocol ----------------------------------------------------------


def test_output_tool_paging_and_grep(client: StdioClient) -> None:
    result = client.run(
        'for i in $(seq 1 18000); do if [ $((i % 6000)) -eq 0 ]; then echo "FAIL case $i"; else echo "ok case $i"; fi; done'
    )
    body = text(result)
    assert len(body) <= 12_500
    handle = result["structuredContent"]["handle"]
    assert f"handle {handle}" in body.splitlines()[1]
    assert f'output(handle="{handle}", offset=' in body
    page = client.tool("output", {"handle": handle, "offset": 1000, "limit": 50})
    lines = text(page).splitlines()
    assert "lines 1,000-1,049" in lines[0] and "18,000 lines total" in lines[0]
    assert lines[1] == "1000: ok case 1000" and len(lines) == 51
    grep = client.tool("output", {"handle": handle, "grep": "FAIL", "context": 2})
    assert "3 matching lines" in text(grep)
    assert "6000: FAIL case 6000" in text(grep) and "5998- ok case 5998" in text(grep)
    tail = client.tool("output", {"handle": handle, "tail": "3"})
    assert body_lines(tail) == [
        "17998: ok case 17998",
        "17999: ok case 17999",
        "18000: FAIL case 18000",
    ]
    bad = client.tool("output", {"handle": handle, "grep": "(["})
    assert bad["isError"] is True and "invalid regular expression" in text(bad)
    missing = client.tool("output", {"handle": "out_00000000"})
    assert missing["isError"] is True and "unknown or expired" in text(missing)
    assert client.tool("output", {"handle": handle})["structuredContent"]["mode"] == "tail"


# --- progress, cancellation, shutdown, crash safety --------------------------------------------


@pytest.mark.slow
def test_progress_notifications(client: StdioClient) -> None:
    rid = client.request(
        "tools/call",
        {
            "name": "run",
            "arguments": {"command": "for i in 1 2 3 4; do echo tick $i; sleep 3; done"},
            "_meta": {"progressToken": "tok"},
        },
    )
    response = client.response(rid, 60)
    assert text(response["result"]).startswith("exit 0")
    progress = [m for m in client.notifications if m.get("method") == "notifications/progress"]
    assert len(progress) >= 2
    values = [m["params"]["progress"] for m in progress]
    assert values == sorted(values)
    assert "tick" in progress[-1]["params"]["message"]
    client.notifications.clear()
    client.run("sleep 6")
    assert not [m for m in client.notifications if m.get("method") == "notifications/progress"]


def test_cancellation_kills_tree(client: StdioClient) -> None:
    marker = unique_sleep()
    rid = client.request(
        "tools/call",
        {
            "name": "run",
            "arguments": {"command": f"bash -c 'sleep {marker} & sleep {marker}; wait' & wait"},
        },
    )

    def sleeps() -> list[str]:
        return [
            line for line in pgrep(f"sleep {marker}") if line.split(None, 1)[1].startswith("sleep")
        ]

    deadline = time.monotonic() + 5
    while len(sleeps()) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(sleeps()) == 2  # child and grandchild of the shell
    client.notify("notifications/cancelled", {"requestId": rid, "reason": "test"})
    assert gone(f"sleep {marker}")
    ping = client.call("ping")
    assert "result" in ping
    assert rid not in client._responses  # pyright: ignore[reportPrivateUsage]


def test_stdin_eof_terminates_children(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    marker = unique_sleep()
    c.initialize()
    c.request("tools/call", {"name": "run", "arguments": {"command": f"sleep {marker}"}})
    deadline = time.monotonic() + 5
    while not pgrep(f"sleep {marker}") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert c.close() == 0
    assert gone(f"sleep {marker}")


@linux_only
def test_sigkill_of_envelope_kills_children(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    marker = unique_sleep()
    c.initialize()
    c.request("tools/call", {"name": "run", "arguments": {"command": f"sleep {marker}"}})
    deadline = time.monotonic() + 5
    while not pgrep(f"sleep {marker}") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert pgrep(f"sleep {marker}")
    os.kill(c.proc.pid, signal.SIGKILL)
    c.proc.wait(5)
    assert gone(f"sleep {marker}")


def test_stdout_is_protocol_only(client: StdioClient) -> None:
    client.run("echo hi")
    client.send_raw(b"{broken")
    assert client.next_message()["error"]["code"] == -32700
    for line in client.raw_lines:
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"


def test_malformed_input_over_stdio(tmp_path: Path) -> None:
    c = StdioClient(
        tmp_path, config='default_shell = "bash"\n', env={"ENVELOPE_LOG_LEVEL": "debug"}
    )
    try:
        c.send_raw(b"not json at all")
        assert c.next_message()["error"]["code"] == -32700
        c.send_raw(b'{"jsonrpc":"2.0","id":1}')
        assert c.next_message()["error"]["code"] == -32600
        assert c.call("nope/method")["error"]["code"] == -32601
        assert c.initialize()["result"]["protocolVersion"] == "2025-11-25"
    finally:
        assert c.close() == 0
    stderr = c.stderr.decode()
    assert "shell bash:" in stderr and "<- initialize" in stderr  # debug: detection + traffic


def test_tools_list_protocol(client: StdioClient) -> None:
    first = client.call("tools/list")
    second = client.call("tools/list")
    assert first["result"] == second["result"]
    tools = {t["name"]: t for t in first["result"]["tools"]}
    assert list(tools) == ["run", "output"]
    run = tools["run"]
    for word in ("shell", "command", "terminal", "bash"):
        assert word in run["description"].lower()
    assert run["annotations"]["readOnlyHint"] is False
    assert run["annotations"]["destructiveHint"] is True
    assert run["annotations"]["openWorldHint"] is True
    assert tools["output"]["annotations"]["readOnlyHint"] is True
    props = run["inputSchema"]["properties"]
    assert all(p.get("type") != "object" for n, p in props.items() if n != "env")
    assert run["inputSchema"]["required"] == ["command"]


def test_stateless_client(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    try:
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        result = c.call(
            "tools/call", {"name": "run", "arguments": {"command": "echo stateless"}, "_meta": meta}
        )
        assert result["result"]["resultType"] == "complete"
        assert result["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "envelope"
        listing = c.call("tools/list", {"_meta": meta})["result"]
        assert listing["ttlMs"] > 0 and listing["cacheScope"] == "private"
    finally:
        c.close()


def test_subprocess_helper_leaves_nothing(tmp_path: Path) -> None:
    # Sanity check for the pgrep helper used above.
    marker = unique_sleep()
    proc = subprocess.Popen(["sleep", marker])
    assert pgrep(f"sleep {marker}")
    proc.kill()
    proc.wait()
    assert gone(f"sleep {marker}")


def test_sigpipe_default_in_commands(client: StdioClient) -> None:
    started = time.monotonic()
    result = client.run("yes | head -n 1")
    assert body_lines(result) == ["y"]
    assert "Broken pipe" not in text(result)
    assert time.monotonic() - started < 10


@linux_only
def test_shim_survives_repeated_sigterm(tmp_path: Path) -> None:
    from envelope.execution.posix import PosixBackend

    marker = unique_sleep()
    backend = PosixBackend()
    script = tmp_path / "s.sh"
    script.write_text(f"sleep {marker} & sleep {marker}\n")
    process = backend.spawn(
        shell="bash",
        args=["/bin/bash", str(script)],
        cwd=str(tmp_path),
        env=dict(os.environ),
        stdin=False,
    )
    deadline = time.monotonic() + 5
    while len(pgrep(f"sleep {marker}")) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    shim = process._popen  # pyright: ignore[reportPrivateUsage]
    for _ in range(5):
        shim.send_signal(signal.SIGTERM)
    assert process.wait_terminated(5)
    assert gone(f"sleep {marker}")
    process.close()
