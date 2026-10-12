from envelope_eval.runner.attribution import Evidence, attribute
from envelope_eval.runner.metrics import Event, lever_metrics, process_metrics, timeline_attribution


def test_attribution_rules():
    assert attribute(Evidence(timed_out=True, tool_in_flight=True)) == "tool"
    assert attribute(Evidence(upstream_error=True)) == "infra"
    assert attribute(Evidence(runaway=True)) == "model"
    assert attribute(Evidence(timed_out=True, request_in_flight=True)) == "harness"
    assert attribute(Evidence(upstream_error=True, runaway=True)) == "indeterminate"
    assert (
        timeline_attribution(
            [Event(0, "tool_start", "tool", {}), Event(1, "timeout", "harness", {})]
        )
        == "tool"
    )
    assert (
        timeline_attribution(
            [
                Event(0, "gateway_error", "model", {"recovered": True}),
                Event(1, "timeout", "harness", {}),
            ]
        )
        == "model"
    )
    assert (
        timeline_attribution(
            [Event(0, "gateway_error", "model", {}), Event(1, "timeout", "harness", {})]
        )
        == "infra"
    )
    assert (
        timeline_attribution(
            [
                Event(0, "gateway_error", "model", {"recovered": True}),
                Event(1, "gateway_error", "model", {"recovered": False}),
                Event(2, "timeout", "harness", {}),
            ]
        )
        == "infra"
    )
    assert (
        timeline_attribution(
            [Event(0, "harness_error", "harness", {}), Event(1, "timeout", "harness", {})]
        )
        == "harness"
    )


def test_timeline_and_levers():
    events = [
        Event(0, "request_start", "model", {}),
        Event(1, "request_end", "model", {}),
        Event(2, "timeout", "harness", {}),
    ]
    assert timeline_attribution(events) == "harness"
    metrics = lever_metrics(
        [{"kind": "hint"}, {"kind": "model_request", "tokens": 3}],
        [
            {"type": "tool_call", "name": "run", "arguments": {"command": "x"}},
            {"type": "tool_call", "name": "output"},
            {"text": "replacement \ufffd"},
        ],
    )
    assert (
        metrics["recovery_via_output"]
        and metrics["replacement_characters"] == 1
        and metrics["main_model_tokens"] == 3
    )


def test_s5_fixture_can_be_parsed():
    from pathlib import Path

    fixture = next(Path("eval/tests/fixtures/s5").rglob("*.stdout"))
    metrics = lever_metrics([], [{"text": fixture.read_text(encoding="utf-8", errors="replace")}])
    assert "replacement_characters" in metrics and "tool_calls" in metrics


def test_background_metrics_from_audit_log():
    rows = [
        {"event": "tool_call", "tool": "run", "outcome": {"state": "running", "handle": "out_a"}},
        {"event": "tool_call", "tool": "run", "outcome": {"state": "running", "handle": "out_b"}},
        {
            "event": "tool_call",
            "tool": "run",
            "executes": False,
            "wait": {"condition": "regex:ready", "outcome": "condition_met"},
            "outcome": {},
        },
        {
            "event": "tool_call",
            "tool": "output",
            "outcome": {"wait": {"condition": "exit", "outcome": "wait_expired"}},
        },
        {"event": "tool_call", "tool": "stop", "outcome": {"handle": "out_a"}},
        {"event": "tool_call", "tool": "processes", "outcome": {}},
        {"event": "process_end", "handle": "out_a", "end_reason": "eviction"},
        {"event": "process_end", "handle": "out_b", "end_reason": "shutdown"},
        {"event": "completion_notice", "handle": "out_a"},
    ]
    metrics = process_metrics(rows)
    assert metrics["handed_back_runs"] == 2
    assert metrics["joins"] == 1
    assert metrics["waits"] == [
        {"condition": "regex:ready", "outcome": "condition_met"},
        {"condition": "exit", "outcome": "wait_expired"},
    ]
    assert metrics["stop_calls"] == metrics["processes_calls"] == 1
    assert metrics["evictions"] == metrics["notices"] == 1
    assert metrics["running_at_agent_end"] == 1
