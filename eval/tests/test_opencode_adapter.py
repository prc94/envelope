"""OpenCode adapter: its config, command line and transcript parser (real S5 transcripts)."""

import json
from pathlib import Path

from envelope_eval.adapters import OpenCodeAdapter
from envelope_eval.adapters.opencode import BUILTIN_TOOLS

S5 = Path(__file__).parent / "fixtures" / "s5" / "logs"
ENVELOPE_TOOLS = ["envelope_run", "envelope_output"]


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
    assert cfg["tools"] == dict.fromkeys(BUILTIN_TOOLS, False)
    assert cfg["permission"] == {"bash": "deny", "edit": "deny", "webfetch": "deny"}
    server = cfg["mcp"]["envelope"]
    assert server["command"] == ["python", "/opt/envelope.pyz"] and server["enabled"] is True
    assert server["environment"]["ENVELOPE_AUDIT_LOG"] == "/w/audit.jsonl"
    assert cfg["autoupdate"] is False and cfg["share"] == "disabled"


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
        "-m", "gateway/qwen/qwen3.8-flash", "Do it.\nCarefully.",
    ]  # fmt: skip
    assert adapter.implicit_tools("envelope") == set()
    assert adapter.reports_tools is False


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
