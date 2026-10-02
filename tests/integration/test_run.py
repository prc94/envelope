"""End-to-end scenarios over the real stdio protocol (POSIX hosts)."""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

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


def process_ends(client: StdioClient, within: float = 5.0) -> list[dict[str, Any]]:
    """Poll the audit log for `process_end` records: writing one happens
    just after the kill is confirmed, so a just-dead process (per `gone()`)
    may not have its record flushed to disk quite yet."""
    deadline = time.monotonic() + within
    while True:
        ends = [r for r in client.audit() if r.get("event") == "process_end"]
        if ends or time.monotonic() >= deadline:
            return ends
        time.sleep(0.1)


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


def test_dev_tty_fails_fast(client: StdioClient) -> None:
    # design B13: the shim starts the shell in a new session (no controlling
    # terminal), so a program that opens /dev/tty directly (bypassing stdin)
    # fails immediately instead of hanging the call.
    started = time.monotonic()
    result = client.run('exec 3< /dev/tty 2>&1 || echo "no tty: $?"', timeout=10)
    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert "no tty" in text(result)


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


def test_coercion_of_wait_s_and_restart_strings(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.tool(
        "run", {"command": f"sleep {marker}", "name": "svc", "wait_s": "0.5", "timeout_s": "30"}
    )
    assert started["structuredContent"]["state"] == "running"
    record = [r for r in client.audit() if r.get("event") == "tool_call"][-1]
    coerced = {c["param"] for c in record["coercions"]}
    assert "wait_s" in coerced and "timeout_s" in coerced

    restarted = client.tool(
        "run", {"command": "echo hi", "name": "svc", "restart": "true", "wait_s": "1"}
    )
    assert "isError" not in restarted
    record = [r for r in client.audit() if r.get("event") == "tool_call"][-1]
    coerced = {c["param"] for c in record["coercions"]}
    assert "restart" in coerced and "wait_s" in coerced


# --- timeouts, trees, concurrency ----------------------------------------------------------


def test_timeout_fires_after_hand_back_and_logs_process_end(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    marker = unique_sleep()
    c.initialize()
    result = c.run(f"sleep {marker}", wait_s=1, timeout_s=2)
    assert result["structuredContent"]["state"] == "running"
    assert pgrep(f"sleep {marker}")  # handed back: timeout_s has not fired yet
    assert gone(f"sleep {marker}", within=6)  # the deadline fires with nobody watching
    ends = process_ends(c)
    assert len(ends) == 1 and ends[0]["end_reason"] == "timeout"
    c.close()


def test_no_default_timeout_lets_a_handed_back_process_keep_running(
    client: StdioClient,
) -> None:
    marker = unique_sleep()
    result = client.run(f"sleep {marker}", wait_s=1)
    assert result["structuredContent"]["state"] == "running"
    time.sleep(3)
    assert pgrep(f"sleep {marker}")  # no default_timeout_s configured: still alive


def test_configured_default_timeout_kills_a_handed_back_process(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\ndefault_timeout_s = 2\n')
    marker = unique_sleep()
    c.initialize()
    result = c.run(f"sleep {marker}", wait_s=1)
    assert result["structuredContent"]["state"] == "running"
    assert gone(f"sleep {marker}", within=6)
    ends = process_ends(c)
    assert len(ends) == 1 and ends[0]["end_reason"] == "timeout"
    c.close()


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


def test_short_command_result_is_unchanged_by_wait_s(client: StdioClient) -> None:
    result = client.run("echo hi")
    assert text(result).startswith("exit 0")
    assert result["structuredContent"]["state"] == "finished"
    assert body_lines(result) == ["hi"]


def test_wait_s_yields_a_running_result_with_a_handle(client: StdioClient) -> None:
    marker = unique_sleep()
    started = time.monotonic()
    result = client.run(f"echo started; sleep {marker}", wait_s=2, timeout_s=30)
    elapsed = time.monotonic() - started
    assert 1.5 < elapsed < 5
    assert text(result).startswith("running")
    assert result["structuredContent"]["state"] == "running"
    handle = result["structuredContent"]["handle"]
    assert f'output(handle="{handle}")' in text(result)
    assert "do not start it again" in text(result)
    assert "started" in body_lines(result)
    assert pgrep(f"sleep {marker}")  # still running: wait_s does not kill it
    assert client.tool("output", {"handle": handle, "tail": 5})


def test_wait_s_zero_checks_without_blocking(client: StdioClient) -> None:
    marker = unique_sleep()
    started = time.monotonic()
    result = client.run(f"sleep {marker}", wait_s=0, timeout_s=30)
    elapsed = time.monotonic() - started
    assert elapsed < 1
    assert result["structuredContent"]["state"] == "running"
    assert pgrep(f"sleep {marker}")


@pytest.mark.slow
def test_default_wait_hands_back_a_long_command(client: StdioClient) -> None:
    # Mirrors the eval suite's `interop-long` task: a command that outlives
    # the default 30s wait is handed back, and later finishes in its handle.
    # The sleep itself must actually run a bit past 30s (unlike `unique_sleep()`,
    # whose value is a pgrep marker, not a real duration).
    marker = f"34.{random.randint(1000, 9999)}"
    started = time.monotonic()
    result = client.run(f"sleep {marker}; echo done", timeout=60)
    elapsed = time.monotonic() - started
    assert 28 < elapsed < 40
    assert result["structuredContent"]["state"] == "running"
    handle = result["structuredContent"]["handle"]
    assert pgrep(f"sleep {marker}")
    assert gone(f"sleep {marker}", within=15.0)
    follow_up = client.tool("output", {"handle": handle, "tail": 5})
    assert any("done" in line for line in body_lines(follow_up))


def test_wait_s_is_clamped_to_the_maximum(client: StdioClient) -> None:
    result = client.run("echo hi", wait_s=100_000)
    assert "wait_s 100000 exceeds the maximum" in text(result)
    assert "clamped to" in text(result)


# --- wait_for conditions (design B5, task 5.2) -------------------------------------------------


def test_wait_for_malformed_condition_is_a_tool_error(client: StdioClient) -> None:
    result = client.run("echo hi", wait_for="port:not-a-number")
    assert result.get("isError") is True
    assert "wait_for" in text(result)


def test_wait_for_regex_condition_met(client: StdioClient) -> None:
    marker = unique_sleep()
    started = time.monotonic()
    result = client.run(
        f"echo waiting; sleep 2; echo ready; sleep {marker}",
        wait_for="regex:ready",
        wait_s=30,
        timeout_s=30,
    )
    elapsed = time.monotonic() - started
    assert 1.5 < elapsed < 10
    assert result["structuredContent"]["state"] == "running"
    assert result["structuredContent"]["wait_outcome"] == "condition_met"
    assert "wait condition met" in text(result)
    assert pgrep(f"sleep {marker}")


def test_wait_for_regex_already_matched_in_bare_form(client: StdioClient) -> None:
    result = client.run("echo ready-now", wait_for="ready-now", wait_s=5)
    assert result["structuredContent"]["state"] == "finished"
    assert result["structuredContent"]["wait_outcome"] == "condition_met"


def test_wait_for_idle_condition_met(client: StdioClient) -> None:
    marker = unique_sleep()
    started = time.monotonic()
    result = client.run(f"echo first; sleep {marker}", wait_for="idle:2", wait_s=30, timeout_s=30)
    elapsed = time.monotonic() - started
    assert 1.5 < elapsed < 15
    assert result["structuredContent"]["state"] == "running"
    assert result["structuredContent"]["wait_outcome"] == "condition_met"
    assert pgrep(f"sleep {marker}")


def test_wait_for_process_exits_before_condition(client: StdioClient) -> None:
    result = client.run("echo only-this-line-here", wait_for="regex:never-appears", wait_s=5)
    assert result["structuredContent"]["state"] == "finished"
    assert result["structuredContent"]["wait_outcome"] == "finished"
    assert "finished before" in text(result)


def test_wait_for_expires_without_error(client: StdioClient) -> None:
    marker = unique_sleep()
    result = client.run(f"sleep {marker}", wait_for="regex:never-appears", wait_s=1, timeout_s=30)
    assert "isError" not in result
    assert result["structuredContent"]["state"] == "running"
    assert result["structuredContent"]["wait_outcome"] == "wait_expired"
    assert "wait expired" in text(result)
    assert pgrep(f"sleep {marker}")


def test_wait_for_port_condition_met(client: StdioClient) -> None:
    # The server is left running for the test client's teardown to clean up
    # (envelope's shutdown path kills every live process); `stop` (task 6.5)
    # doesn't exist yet.
    port = random.randint(20000, 40000)
    started = time.monotonic()
    result = client.run(
        f"python3 -m http.server {port} --bind 127.0.0.1",
        wait_for=f"port:{port}",
        wait_s=15,
        timeout_s=30,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 15
    assert result["structuredContent"]["state"] == "running"
    assert result["structuredContent"]["wait_outcome"] == "condition_met"
    assert "port ownership could not be verified" not in text(result)


# --- names, joins and restart (design B6, task 6.1) --------------------------------------------


def test_name_idempotent_start(client: StdioClient) -> None:
    marker = unique_sleep()
    first = client.run(f"sleep {marker}", name="web", wait_s=1, timeout_s=30)
    assert first["structuredContent"]["state"] == "running"
    handle = first["structuredContent"]["handle"]

    second = client.run(f"sleep {marker}", name="web", wait_s=1, timeout_s=30)
    assert second["structuredContent"]["handle"] == handle
    assert len(pgrep(f"sleep {marker}")) == 1


def test_name_parallel_starts_create_one_process(client: StdioClient) -> None:
    marker = unique_sleep()
    ids = [
        client.request(
            "tools/call",
            {
                "name": "run",
                "arguments": {
                    "command": f"sleep {marker}",
                    "name": "web",
                    "wait_s": 1,
                    "timeout_s": 30,
                },
            },
        )
        for _ in range(2)
    ]
    results = [client.response(rid, 30) for rid in ids]
    handles = {r["result"]["structuredContent"]["handle"] for r in results}
    assert len(handles) == 1
    assert len(pgrep(f"sleep {marker}")) == 1


def test_name_after_exit(client: StdioClient) -> None:
    result = client.run("exit 1", name="tests", wait_s=5)
    assert result["structuredContent"]["state"] == "finished"
    assert result["structuredContent"]["exit_code"] == 1


def test_invalid_name_is_a_tool_error(client: StdioClient) -> None:
    result = client.run("echo hi", name="my server!")
    assert result.get("isError") is True
    assert "name" in text(result)

    handle_shaped = client.run("echo hi", name="out_1234abcd")
    assert handle_shaped.get("isError") is True


def test_restart_with_changed_command(client: StdioClient) -> None:
    marker_a = unique_sleep()
    marker_b = unique_sleep()
    first = client.run(f"sleep {marker_a}", name="web", wait_s=1, timeout_s=30)
    assert first["structuredContent"]["state"] == "running"
    old_handle = first["structuredContent"]["handle"]
    assert pgrep(f"sleep {marker_a}")

    second = client.run(f"sleep {marker_b}", name="web", restart=True, wait_s=1, timeout_s=30)
    assert second["structuredContent"]["state"] == "running"
    assert second["structuredContent"]["handle"] != old_handle
    assert gone(f"sleep {marker_a}")
    assert pgrep(f"sleep {marker_b}")


def test_restart_denied_leaves_the_process_running(client: StdioClient) -> None:
    marker = unique_sleep()
    first = client.run(f"sleep {marker}", name="web", wait_s=1, timeout_s=30)
    assert first["structuredContent"]["state"] == "running"

    denied = client.run("echo hi", name="web", restart=True, cwd="/no/such/directory")
    assert denied.get("isError") is True
    assert pgrep(f"sleep {marker}")


# --- output tool: wait_s/wait_for, name handles, process state (task 6.2) -----------------------


def test_output_by_name_and_process_state_running(client: StdioClient) -> None:
    marker = unique_sleep()
    first = client.run(f"sleep {marker}", name="web", wait_s=1, timeout_s=30)
    assert first["structuredContent"]["state"] == "running"

    result = client.tool("output", {"handle": "web", "tail": 5})
    assert result["structuredContent"]["handle"] == first["structuredContent"]["handle"]
    assert result["structuredContent"]["state"] == "running"
    assert result["structuredContent"]["name"] == "web"


def test_output_process_state_ended(client: StdioClient) -> None:
    first = client.run("exit 3", name="tests", wait_s=5)
    assert first["structuredContent"]["state"] == "finished"

    result = client.tool("output", {"handle": "tests"})
    assert result["structuredContent"]["state"] == "ended"
    assert result["structuredContent"]["exit_code"] == 3


def test_output_wait_for_regex_from_unread_backlog(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(f"echo ready; sleep {marker}", wait_s=1, timeout_s=30, name="watcher")
    assert started["structuredContent"]["state"] == "running"

    result = client.tool("output", {"handle": "watcher", "wait_for": "regex:ready", "wait_s": 5})
    assert result["structuredContent"]["wait_outcome"] == "condition_met"
    assert "wait condition met" in text(result)


def test_output_wait_for_regex_appears_later(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(
        f"sleep 2; echo later-line; sleep {marker}", wait_s=0.5, timeout_s=30, name="watcher2"
    )
    assert started["structuredContent"]["state"] == "running"
    begin = time.monotonic()
    result = client.tool(
        "output", {"handle": "watcher2", "wait_for": "regex:later-line", "wait_s": 10}
    )
    elapsed = time.monotonic() - begin
    assert 1 < elapsed < 10
    assert result["structuredContent"]["wait_outcome"] == "condition_met"


def test_output_wait_expires_without_error(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(f"sleep {marker}", wait_s=0.5, timeout_s=30, name="watcher3")
    assert started["structuredContent"]["state"] == "running"
    result = client.tool(
        "output", {"handle": "watcher3", "wait_for": "regex:never-appears", "wait_s": 1}
    )
    assert "isError" not in result
    assert result["structuredContent"]["wait_outcome"] == "wait_expired"


# --- stop and processes tools (task 6.5) --------------------------------------------------------


def test_stop_graceful_on_a_trapping_script(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(
        f"trap 'echo trapped; exit 7' TERM; echo ready; sleep {marker}",
        wait_s=1,
        timeout_s=30,
    )
    assert started["structuredContent"]["state"] == "running"
    handle = started["structuredContent"]["handle"]

    result = client.tool("stop", {"handle": handle, "grace_s": 5})
    assert result["structuredContent"]["exit_code"] == 7
    assert result["structuredContent"]["graceful"] is True
    assert "graceful" in text(result)
    assert gone(f"sleep {marker}")


def test_stop_forced_on_an_unresponsive_process(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(f"trap '' TERM; sleep {marker}", wait_s=1, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"
    handle = started["structuredContent"]["handle"]

    result = client.tool("stop", {"handle": handle, "grace_s": 1}, timeout=15)
    assert result["structuredContent"]["graceful"] is False
    assert "forced" in text(result)
    assert gone(f"sleep {marker}")


def test_stop_already_finished_is_not_an_error(client: StdioClient) -> None:
    first = client.run("exit 0")
    assert first["structuredContent"]["state"] == "finished"
    handle = first["structuredContent"]["handle"]

    result = client.tool("stop", {"handle": handle})
    assert "isError" not in result
    assert result["structuredContent"]["exit_code"] == 0


def test_stop_immediately_after_spawn(client: StdioClient) -> None:
    # The shim installs its SIGUSR1 handler before writing the `pid` status
    # line and blocks it outside the wait loop (design B7), so a stop raced
    # right against spawn must still be delivered once the shell exists,
    # not dropped or racily kill the shim itself.
    marker = unique_sleep()
    started = client.run(f"sleep {marker}", wait_s=0, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"
    handle = started["structuredContent"]["handle"]

    result = client.tool("stop", {"handle": handle, "grace_s": 5})
    assert "isError" not in result
    assert gone(f"sleep {marker}")


def test_stop_by_name(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(f"sleep {marker}", name="svc", wait_s=1, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"

    result = client.tool("stop", {"handle": "svc"})
    assert "isError" not in result
    assert gone(f"sleep {marker}")


def test_processes_lists_running_and_finished(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(f"sleep {marker}", name="web", wait_s=1, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"

    result = client.tool("processes", {})
    structured = result["structuredContent"]
    handles = {p["handle"] for p in structured["processes"]}
    assert started["structuredContent"]["handle"] in handles
    web_entry = next(
        p for p in structured["processes"] if p["handle"] == started["structuredContent"]["handle"]
    )
    assert web_entry["state"] == "running"
    assert web_entry["name"] == "web"
    client.tool("stop", {"handle": "web"})


def test_processes_nothing_running(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    c.initialize()
    try:
        result = c.tool("processes", {})
        assert "no processes" in text(result)
        assert result["structuredContent"]["count"] == 0
    finally:
        c.close()


# --- completion notices (design B10, task 7.2) --------------------------------------------------


def test_completion_notice_on_an_unrelated_call(client: StdioClient) -> None:
    started = client.run("sleep 1.5; exit 1", wait_s=0.2, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"
    handle = started["structuredContent"]["handle"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        unrelated = client.run("echo poke")
        notices = unrelated["structuredContent"].get("notices")
        if notices:
            assert any(handle in n and "exit 1" in n for n in notices)
            break
        time.sleep(0.2)
    else:
        raise AssertionError("no completion notice seen within 10s")
    # it must not be reported a second time
    again = client.run("echo poke again")
    assert not again["structuredContent"].get("notices")


def test_same_command_note_for_a_duplicate_unnamed_start(client: StdioClient) -> None:
    marker = unique_sleep()
    command = f"sleep {marker}"
    first = client.run(command, wait_s=1, timeout_s=30)
    assert first["structuredContent"]["state"] == "running"
    handle = first["structuredContent"]["handle"]

    second = client.run(command, wait_s=1, timeout_s=30)
    assert f"{handle} is still running this exact command" in text(second)
    client.tool("output", {"handle": handle})
    client.tool("output", {"handle": second["structuredContent"]["handle"]})


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
    assert body_lines(result)[0] == "done"
    assert result["structuredContent"]["timed_out"] is False
    assert gone(f"sleep {marker}")
    assert "background_job_killed" in result["structuredContent"].get("hints", [])
    assert "background process" in text(result)


def test_background_job_killed_hint_suppressed_while_stopping(client: StdioClient) -> None:
    marker_a, marker_b = unique_sleep(), unique_sleep()
    started = client.run(f"sleep {marker_a} & sleep {marker_b}", wait_s=0.5, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"
    handle = started["structuredContent"]["handle"]
    result = client.tool("stop", {"handle": handle, "grace_s": 1})
    assert "background_job_killed" not in result["structuredContent"].get("hints", [])
    assert gone(f"sleep {marker_a}") and gone(f"sleep {marker_b}")


def test_port_in_use_hint_names_envelope_owner(client: StdioClient) -> None:
    port = random.randint(20000, 40000)
    owner = client.run(
        f"python3 -m http.server {port} --bind 127.0.0.1",
        name="web",
        wait_for=f"port:{port}",
        wait_s=15,
        timeout_s=30,
    )
    assert owner["structuredContent"]["wait_outcome"] == "condition_met"

    # A real `http.server` bind conflict doesn't put the port number on the
    # same line as its OSError, so this prints a message shaped like a real
    # tool's (e.g. Node's) address-in-use error, with the port, to exercise
    # the hint's hook into envelope's own port table.
    conflict = client.run(
        f"echo 'Error: listen EADDRINUSE: address already in use :::{port}'; exit 1",
        wait_s=2,
        timeout_s=10,
    )
    assert "port_in_use" in conflict["structuredContent"].get("hints", [])
    assert "web" in text(conflict) and str(port) in text(conflict)


# --- audit records (design B15, task 7.5) --------------------------------------------------


def _tool_calls(records: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == "tool_call" and r.get("tool") == tool]


def test_audit_records_hand_back_join_stop_and_eviction(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\nmax_running = 1\n')
    c.initialize()
    try:
        marker_a = unique_sleep()
        handed_back = c.run(f"sleep {marker_a}", name="svc", wait_s=0.5, timeout_s=30)
        assert handed_back["structuredContent"]["state"] == "running"

        joined = c.run(f"sleep {marker_a}", name="svc", wait_s=0.2, timeout_s=30)
        assert joined["structuredContent"]["handle"] == handed_back["structuredContent"]["handle"]

        c.tool("stop", {"handle": "svc"})

        marker_b = unique_sleep()
        first = c.run(f"sleep {marker_b}", wait_s=0.2, timeout_s=30)
        assert first["structuredContent"]["state"] == "running"
        second = c.run("echo evict-me", wait_s=5, timeout_s=30)
        assert "was evicted" in text(second)
        assert gone(f"sleep {marker_b}")

        records = c.audit()
        run_calls = _tool_calls(records, "run")
        assert any(r["outcome"].get("state") == "running" for r in run_calls)
        joins = [r for r in run_calls if r.get("executes") is False]
        assert joins and joins[0]["decision"] is None
        stop_calls = _tool_calls(records, "stop")
        assert (
            stop_calls
            and stop_calls[0]["outcome"]["handle"] == handed_back["structuredContent"]["handle"]
        )
        process_ends = [r for r in records if r.get("event") == "process_end"]
        assert any(r["end_reason"] == "eviction" for r in process_ends)
    finally:
        c.close()


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


def test_two_concurrent_starts_never_exceed_the_cap(tmp_path: Path) -> None:
    # design B8: calls no longer queue behind a cap; replaces the old
    # semaphore-queueing test now that max_running evicts instead.
    c = StdioClient(tmp_path, config='default_shell = "bash"\nmax_running = 8\n')
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
            for _ in range(8)
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
    assert peak == 8  # all 8 fit under the cap at once: no eviction needed


def test_eviction_frees_a_slot_with_a_note(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\nmax_running = 2\n')
    c.initialize()
    a = unique_sleep()
    b = unique_sleep()
    first = c.run(f"sleep {a}", wait_s=0.5, timeout_s=30)
    second = c.run(f"sleep {b}", wait_s=0.5, timeout_s=30)
    assert first["structuredContent"]["state"] == "running"
    assert second["structuredContent"]["state"] == "running"

    third = c.run("echo third", wait_s=5, timeout_s=30)
    text_third = text(third)
    assert "was evicted" in text_third and "max_running=2" in text_third
    evicted_handle = first["structuredContent"]["handle"]
    assert evicted_handle in text_third  # the LRU one (first) was evicted, not second
    assert gone(f"sleep {a}")
    assert pgrep(f"sleep {b}")  # the more-recently-used one survives
    c.close()


def test_evict_policy_refuse_never_evicts(tmp_path: Path) -> None:
    c = StdioClient(
        tmp_path, config='default_shell = "bash"\nmax_running = 1\nevict_policy = "refuse"\n'
    )
    c.initialize()
    marker = unique_sleep()
    held = c.run(f"sleep {marker}", wait_s=0.5, timeout_s=30)
    assert held["structuredContent"]["state"] == "running"

    refused = c.run("echo second", wait_s=5, timeout_s=30)
    assert "live-process cap" in text(refused)
    assert pgrep(f"sleep {marker}")  # refuse policy: never evicted
    c.close()


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
    # no explicit mode, never read via the default mode before: "unread" from line 1
    # (explicit offset/grep/tail calls above never move the read position)
    unread = client.tool("output", {"handle": handle})
    assert unread["structuredContent"]["mode"] == "unread"
    assert body_lines(unread)[0] == "1: ok case 1"


def test_output_tool_default_mode_is_unread_then_falls_back_to_tail(client: StdioClient) -> None:
    result = client.run("for i in $(seq 1 50); do echo line $i; done")
    handle = result["structuredContent"]["handle"]

    first = client.tool("output", {"handle": handle})
    assert first["structuredContent"]["mode"] == "unread"
    assert body_lines(first) == [f"{i}: line {i}" for i in range(1, 51)]

    # everything has now been shown once via the default mode; the next
    # no-args call has nothing new and falls back to tail
    second = client.tool("output", {"handle": handle})
    assert second["structuredContent"]["mode"] == "tail"


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


def test_cancelling_a_join_only_ends_the_wait(client: StdioClient) -> None:
    marker = unique_sleep()
    started = client.run(f"sleep {marker}", name="web", wait_s=0.5, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"
    assert pgrep(f"sleep {marker}")

    rid = client.request(
        "tools/call",
        {"name": "run", "arguments": {"command": f"sleep {marker}", "name": "web", "wait_s": 20}},
    )
    time.sleep(0.3)
    client.notify("notifications/cancelled", {"requestId": rid, "reason": "test"})
    ping = client.call("ping")
    assert "result" in ping
    assert rid not in client._responses  # pyright: ignore[reportPrivateUsage]
    # the join's cancellation must not have touched the joined process
    assert pgrep(f"sleep {marker}")


def test_cancelling_a_restart_still_stops_old_and_kills_new(client: StdioClient) -> None:
    marker_old, marker_new = unique_sleep(), unique_sleep()
    started = client.run(f"sleep {marker_old}", name="web", wait_s=0.5, timeout_s=30)
    assert started["structuredContent"]["state"] == "running"
    assert pgrep(f"sleep {marker_old}")

    rid = client.request(
        "tools/call",
        {
            "name": "run",
            "arguments": {
                "command": f"sleep {marker_new}",
                "name": "web",
                "restart": True,
                "wait_s": 20,
            },
        },
    )
    assert gone(f"sleep {marker_old}")  # old process stopped despite the cancel below
    client.notify("notifications/cancelled", {"requestId": rid, "reason": "test"})
    assert gone(f"sleep {marker_new}")  # new process's tree killed by the cancel
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


def test_stdin_eof_logs_process_end_for_a_handed_back_process(tmp_path: Path) -> None:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    marker = unique_sleep()
    c.initialize()
    # the request is left in flight on purpose: no response is ever read
    c.request("tools/call", {"name": "run", "arguments": {"command": f"sleep {marker}"}})
    deadline = time.monotonic() + 5
    while not pgrep(f"sleep {marker}") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert pgrep(f"sleep {marker}")
    assert c.close() == 0
    assert gone(f"sleep {marker}")
    ends = [r for r in c.audit() if r.get("event") == "process_end"]
    assert len(ends) == 1 and ends[0]["end_reason"] == "shutdown"


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
    assert list(tools) == ["run", "output", "stop", "processes"]
    run = tools["run"]
    for word in ("shell", "command", "terminal", "bash"):
        assert word in run["description"].lower()
    assert run["annotations"]["readOnlyHint"] is False
    assert run["annotations"]["destructiveHint"] is True
    assert run["annotations"]["openWorldHint"] is True
    assert tools["output"]["annotations"]["readOnlyHint"] is True
    assert tools["stop"]["annotations"]["readOnlyHint"] is False
    assert tools["processes"]["annotations"]["readOnlyHint"] is True
    props = run["inputSchema"]["properties"]
    assert all(p.get("type") != "object" for n, p in props.items() if n not in ("env", "wait_for"))
    assert run["inputSchema"]["required"] == ["command"]
    for name in ("run", "stop", "processes"):
        assert "background" in tools[name]["description"].lower()
        assert "process" in tools[name]["description"].lower()


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
