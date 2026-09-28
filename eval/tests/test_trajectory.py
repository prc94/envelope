from pathlib import Path

from envelope_eval.adapters import QwenAdapter, parse_transcript


def test_parse_wrapped_and_truncated(tmp_path: Path):
    complete = tmp_path / "complete.jsonl"
    complete.write_text(
        '{"event":{"type":"tool_call","tool_call":{"name":"run","arguments":{}}}}\n{"role":"assistant","content":"done"}\n',
        encoding="utf-8",
    )
    trajectory = parse_transcript(complete)
    assert (
        trajectory.events[0]["name"] == "run"
        and trajectory.final_answer == "done"
        and trajectory.truncated is False
    )
    cut = tmp_path / "cut.jsonl"
    cut.write_text('{"role":"assistant","content":"partial"}\n{"bad"', encoding="utf-8")
    assert parse_transcript(cut).truncated
    assert QwenAdapter().parse_trajectory(cut).truncated
    fixture = Path("eval/tests/fixtures/s5/qwen/audit.jsonl")
    assert fixture.exists()
    assert isinstance(parse_transcript(fixture).events, tuple)


STREAM_JSON = [
    {
        "type": "system",
        "subtype": "init",
        "tools": ["tool_search", "tool_call", "mcp__envelope__run"],
    },
    {"type": "stream_event", "event": {"type": "content_block_delta"}},
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "tool_call",
                    "input": {"name": "mcp__envelope__run", "arguments": {"command": "echo hi"}},
                }
            ],
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "is_error": False, "content": "hi"}
            ],
        },
    },
    {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    },
    {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 2},
]


def test_stream_json_nested_blocks_and_the_deferred_tool_wrapper(tmp_path: Path):
    import json

    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in STREAM_JSON), encoding="utf-8")
    trajectory = QwenAdapter().parse_trajectory(path)
    kinds = [e["type"] for e in trajectory.events]
    assert kinds == ["system", "tool_call", "tool_result", "text", "result"]  # no stream_event
    call = trajectory.events[1]
    assert call["name"] == "run" and call["raw_name"] == "mcp__envelope__run"
    assert call["via"] == "tool_call" and call["arguments"] == {"command": "echo hi"}
    assert trajectory.events[2]["id"] == call["id"] and trajectory.events[2]["content"] == "hi"
    assert trajectory.final_answer == "done" and not trajectory.truncated
    assert QwenAdapter().exposed_tools(trajectory) == {
        "tool_search",
        "tool_call",
        "mcp__envelope__run",
    }


def test_stream_json_killed_mid_command_keeps_the_in_flight_call(tmp_path: Path):
    import json

    lines = [json.dumps(e) for e in STREAM_JSON[:3]]
    path = tmp_path / "killed.jsonl"
    path.write_text("\n".join(lines) + '\n{"type": "assist', encoding="utf-8")  # cut mid-line
    trajectory = QwenAdapter().parse_trajectory(path)
    assert trajectory.truncated and [e["type"] for e in trajectory.events] == [
        "system",
        "tool_call",
    ]
    assert trajectory.final_answer == ""


ENVELOPE_TOOLS = ["mcp__envelope__run", "mcp__envelope__output"]


def test_envelope_arm_command_excludes_every_builtin_except_the_deferral_wrappers(tmp_path):
    from envelope_eval.adapters.qwen import BUILTIN_TOOLS

    adapter = QwenAdapter(launch=["node", "cli-entry.js"])
    prompt = 'Line one.\nLine two with "quotes" and %PATH%.'
    argv = adapter.command(prompt, tmp_path, "envelope", ENVELOPE_TOOLS)
    assert argv[:2] == ["node", "cli-entry.js"] and argv[-2:] == ["-p", prompt]  # one argument
    assert "--bare" not in argv  # non-bare: MCP tools are deferred, as for a real user
    for flag in ("--auth-type", "-y", "-o", "stream-json"):
        assert flag in argv
    start = argv.index("--exclude-tools") + 1
    excluded = argv[start : argv.index("-p")]
    assert set(excluded) == set(BUILTIN_TOOLS) - {"tool_search", "tool_call"}
    assert "run_shell_command" in excluded and "agent" in excluded and "read_file" in excluded


def test_native_arm_keeps_only_its_shell_tool(tmp_path):
    adapter = QwenAdapter()
    argv = adapter.command("x", tmp_path, "native", ["run_shell_command"])
    excluded = set(argv[argv.index("--exclude-tools") + 1 : argv.index("-p")])
    assert "run_shell_command" not in excluded and {"tool_search", "tool_call"} <= excluded
    assert adapter.implicit_tools("native") == set()
    assert adapter.visible_tools("native", ["run_shell_command"]) == {"run_shell_command"}


def test_extra_non_shell_tools_are_kept_and_visible(tmp_path):
    adapter = QwenAdapter()
    tools = [*ENVELOPE_TOOLS, "read_file"]
    argv = adapter.command("x", tmp_path, "envelope", tools)
    assert "read_file" not in argv[argv.index("--exclude-tools") + 1 : argv.index("-p")]
    assert adapter.visible_tools("envelope", tools) == {"read_file", "tool_search", "tool_call"}
    assert adapter.implicit_tools("envelope") == {"tool_search", "tool_call"}


def test_settings_turn_off_the_memory_agent_and_configure_envelope_per_trial(tmp_path):
    import json

    adapter = QwenAdapter(envelope_command=["python", "C:\\eval\\envelope.pyz"])
    env = {"ENVELOPE_AUDIT_LOG": "C:\\t\\audit.jsonl", "HOME": "C:\\t\\home"}
    adapter.configure(tmp_path, "qwen/x", "tok", ENVELOPE_TOOLS, "http://127.0.0.1:18787/v1", env)
    settings = json.loads((tmp_path / ".qwen" / "settings.json").read_text())
    assert settings["memory"] == {"enableManagedAutoMemory": False, "enableManagedAutoDream": False}
    server = settings["mcpServers"]["envelope"]
    assert server["command"] == "python" and server["args"] == ["C:\\eval\\envelope.pyz"]
    assert server["env"]["ENVELOPE_AUDIT_LOG"] == "C:\\t\\audit.jsonl"
    assert "tok" not in json.dumps(settings)  # credentials travel in the environment only
    native = tmp_path / "native"
    adapter.configure(native, "qwen/x", "tok", ["run_shell_command"], "http://x/v1", env)
    assert "mcpServers" not in json.loads((native / ".qwen" / "settings.json").read_text())


def test_environment_points_the_harness_at_the_gateway(tmp_path):
    env = QwenAdapter().environment("qwen/qwen3.8-flash", "tok", "http://127.0.0.1:18787/v1")
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:18787/v1"
    assert env["OPENAI_API_KEY"] == "tok" and env["OPENAI_MODEL"] == "qwen/qwen3.8-flash"
