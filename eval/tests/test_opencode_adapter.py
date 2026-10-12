"""OpenCode adapter: its config, command line and transcript parser (real S5 transcripts)."""

import json
from pathlib import Path

from envelope_eval.adapters import OpenCodeAdapter
from envelope_eval.adapters.opencode import BUILTIN_TOOLS

S5 = Path(__file__).parent / "fixtures" / "s5" / "logs"
ENVELOPE_TOOLS = ["mcp__envelope__run", "mcp__envelope__output"]  # as job files name them


def config_of(tmp_path, tool_set, **kw):
    adapter = OpenCodeAdapter(envelope_command=["python", "/opt/envelope.pyz"])
    adapter.configure(
        tmp_path,
        "qwen/qwen3.8-flash",
        "tok-123",
        tool_set,
        "http://172.17.0.1:8787/v1",
        kw.get("env", {}),
    )
    return adapter, json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())


def test_the_config_points_at_the_gateway_pins_the_small_model_and_keeps_the_token_out(tmp_path):
    adapter, cfg = config_of(tmp_path, ENVELOPE_TOOLS, env={"ENVELOPE_AUDIT_LOG": "/w/audit.jsonl"})
    provider = cfg["provider"]["gateway"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://172.17.0.1:8787/v1"
    assert provider["options"]["apiKey"] == "{env:EVAL_GATEWAY_TOKEN}"
    assert "tok-123" not in json.dumps(cfg)  # the trial token travels in the environment only
    assert (
        cfg["model"] == cfg["small_model"] == "gateway/qwen/qwen3.8-flash"
    )  # no title agent model
    assert set(provider["models"]) == {"qwen/qwen3.8-flash"}
    assert adapter.environment("m", "tok-123", "u")["EVAL_GATEWAY_TOKEN"] == "tok-123"


def test_envelope_arm_disables_every_builtin_tool_and_configures_the_mcp_server(tmp_path):
    _, cfg = config_of(tmp_path, ENVELOPE_TOOLS, env={"ENVELOPE_AUDIT_LOG": "/w/audit.jsonl"})
    assert {t: cfg["tools"][t] for t in BUILTIN_TOOLS} == dict.fromkeys(BUILTIN_TOOLS, False)
    assert cfg["permission"] == {"bash": "deny", "edit": "deny", "webfetch": "deny"}
    server = cfg["mcp"]["envelope"]
    assert server["command"] == ["python", "/opt/envelope.pyz"] and server["enabled"] is True
    assert server["environment"]["ENVELOPE_AUDIT_LOG"] == "/w/audit.jsonl"
    assert cfg["autoupdate"] is False and cfg["share"] == "disabled"


def test_envelope_tools_outside_the_arm_are_switched_off_by_name(tmp_path):
    _, cfg = config_of(tmp_path, ENVELOPE_TOOLS)  # run and output only
    assert cfg["tools"]["envelope_stop"] is False and cfg["tools"]["envelope_processes"] is False
    assert "envelope_run" not in cfg["tools"] and "envelope_output" not in cfg["tools"]
    four = [*ENVELOPE_TOOLS, "mcp__envelope__stop", "mcp__envelope__processes"]
    _, cfg = config_of(tmp_path, four)
    assert not any(name.startswith("envelope_") for name in cfg["tools"])


def test_envelope_tools_are_expected_under_opencodes_own_names(tmp_path):
    adapter, _ = config_of(tmp_path, ENVELOPE_TOOLS)
    assert adapter.visible_tools("envelope", ENVELOPE_TOOLS) == {"envelope_run", "envelope_output"}


def test_native_arm_keeps_only_bash_and_has_no_mcp_server(tmp_path):
    adapter, cfg = config_of(tmp_path, ["bash"])
    assert [t for t, on in cfg["tools"].items() if on] == ["bash"]
    assert cfg["permission"]["bash"] == "allow" and "mcp" not in cfg
    assert adapter.visible_tools("native", ["bash"]) == {"bash"}


def test_command_line_is_headless_json_in_the_workspace_with_the_gateway_model(tmp_path):
    adapter, _ = config_of(tmp_path, ENVELOPE_TOOLS)
    argv = adapter.command("Do it.\nCarefully.", Path("/work/ws"), "envelope", ENVELOPE_TOOLS)
    assert argv == [
        "opencode", "run", "--pure", "--format", "json", "--dir", "/work/ws",
        "-m", "gateway/qwen/qwen3.8-flash",
    ]  # fmt: skip
    assert adapter.implicit_tools("envelope") == set()
    assert adapter.reports_tools is False


def test_instruction_is_sent_verbatim_on_stdin(tmp_path):
    adapter, _ = config_of(tmp_path, ENVELOPE_TOOLS)
    instruction = '  --help\nRun printf "a  b\\n"; sleep 40; then say `готово`.\n\tKeep spaces.  '
    assert adapter.stdin(instruction) == instruction
    assert instruction not in adapter.command(instruction, tmp_path, "envelope", ENVELOPE_TOOLS)


def test_model_prompt_is_opt_in_and_shared_by_envelope_and_native_arms(tmp_path):
    prompt = "Complete the user's task with the available tools."
    adapter = OpenCodeAdapter(model_prompts={"qwen/qwen3.8-flash": prompt})
    for tool_set in (ENVELOPE_TOOLS, ["bash"]):
        adapter.configure(tmp_path, "qwen/qwen3.8-flash", "token", tool_set, "http://local/v1")
        cfg = json.loads((tmp_path / ".config/opencode/opencode.json").read_text())
        assert cfg["agent"]["build"]["prompt"] == prompt
    adapter.configure(tmp_path, "other/model", "token", ENVELOPE_TOOLS, "http://local/v1")
    cfg = json.loads((tmp_path / ".config/opencode/opencode.json").read_text())
    assert "agent" not in cfg


def test_real_transcripts_parse_into_calls_results_and_the_final_answer():
    basic = OpenCodeAdapter().parse_trajectory(
        S5 / "opencode-deepseek_deepseek-v4-flash-0731-basic.stdout"
    )
    kinds = [e["type"] for e in basic.events]
    assert kinds == ["tool_call", "tool_result", "text"] and not basic.truncated
    call, result, _ = basic.events
    assert call["name"] == "run" and call["raw_name"] == "envelope_run"
    assert call["arguments"] == {"command": "echo interop-$((6*7))"} and result["id"] == call["id"]
    assert "interop-42" in result["content"] and basic.final_answer == "interop-42"
    assert OpenCodeAdapter().exposed_tools(basic) is None  # OpenCode reports no tool list


def test_a_transcript_cut_off_by_a_kill_keeps_what_was_written(tmp_path):
    source = (S5 / "opencode-deepseek_deepseek-v4-flash-0731-basic.stdout").read_text()
    lines = source.splitlines()
    cut = tmp_path / "cut.jsonl"
    cut.write_text("\n".join(lines[:2]) + "\n" + lines[2][: len(lines[2]) // 2], encoding="utf-8")
    trajectory = OpenCodeAdapter().parse_trajectory(cut)
    assert trajectory.truncated and [e["type"] for e in trajectory.events][:2] == [
        "tool_call",
        "tool_result",
    ]
    assert trajectory.final_answer == ""


def test_the_other_s5_transcripts_also_parse():
    for name in ("truncation", "long"):
        trajectory = OpenCodeAdapter().parse_trajectory(
            S5 / f"opencode-deepseek_deepseek-v4-flash-0731-{name}.stdout"
        )
        assert any(e["type"] == "tool_call" for e in trajectory.events), name
        assert not trajectory.truncated and trajectory.final_answer, name
