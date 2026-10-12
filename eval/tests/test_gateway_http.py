import http.client
import json
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from gateway.gateway import (
    AdminServer,
    GatewayServer,
    Ledger,
    TokenRegistry,
    scan_credentials,
    validate_bind_address,
)

KEY = "sk-or-test-KEY-0123456789"


class FakeUpstream(ThreadingHTTPServer):
    """Stands in for OpenRouter; `script(handler, body)` writes the response."""

    def __init__(self, script: Callable[[BaseHTTPRequestHandler, dict[str, Any]], None]):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.script = script
        self.models: list[str] | None = None  # served by /api/v1/models/user; None: HTTP 404
        self.seen: list[dict[str, Any]] = []
        self.auth: list[str | None] = []
        self.gets: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.daemon_threads = True


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        upstream = cast("FakeUpstream", self.server)
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        upstream.seen.append(body)
        upstream.auth.append(self.headers.get("Authorization"))
        upstream.headers.append(dict(self.headers.items()))
        upstream.script(self, body)

    def do_GET(self):
        upstream = cast("FakeUpstream", self.server)
        upstream.auth.append(self.headers.get("Authorization"))
        upstream.headers.append(dict(self.headers.items()))
        upstream.gets.append(self.path)
        if self.path != "/api/v1/models/user" or upstream.models is None:
            data, status = b'{"error":{"message":"not found"}}', 404
        else:
            data, status = json.dumps({"data": [{"id": m} for m in upstream.models]}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def plain(handler, body, model="served-model", provider="p", status=200, extra=None):
    payload = {
        "id": "x",
        "model": model,
        "provider": provider,
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 2},
            "cost": 0.5,
        },
    } | (extra or {})
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def sse(handler, *chunks, pause=0.0):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for chunk in chunks:
        if isinstance(chunk, float):
            time.sleep(chunk)
            continue
        handler.wfile.write(chunk)
        handler.wfile.flush()
        time.sleep(pause)


def event(**value):
    return b"data: " + json.dumps(value).encode() + b"\n\n"


class Rig:
    def __init__(self, tmp_path: Path, script, **kwargs):
        kwargs.setdefault("retries", 0)  # tests opt in to retries (and fake the waiting)
        try:
            self.upstream = FakeUpstream(script)
        except PermissionError:
            pytest.skip("sandbox disallows loopback sockets")
        self.registry = TokenRegistry()
        self.ledger_path = tmp_path / "ledger.jsonl"
        self.gateway = GatewayServer(
            ("127.0.0.1", 0),
            ("127.0.0.1", self.upstream.server_port),
            self.registry,
            Ledger(self.ledger_path),
            api_key=KEY,
            **kwargs,
        )
        for server in (self.upstream, self.gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(self, token, body, path="/v1/chat/completions"):
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        conn.request("POST", path, json.dumps(body), headers)
        response = conn.getresponse()
        return response.status, response.read()

    def records(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text().splitlines()]

    def close(self):
        self.upstream.shutdown()
        self.gateway.shutdown()


@pytest.fixture
def rig(tmp_path):
    made: list[Rig] = []

    def make(script=plain, **kwargs):
        made.append(Rig(tmp_path, script, **kwargs))
        return made[-1]

    yield make
    for item in made:
        item.close()


def test_unauthenticated_requests_are_rejected_without_forwarding(rig):
    r = rig()
    good = r.registry.issue("l", "j", "t", ["model"])
    assert r.post(None, {"model": "model"})[0] == 401
    assert r.post("not-issued", {"model": "model"})[0] == 401
    r.registry.revoke(good.token)
    assert r.post(good.token, {"model": "model"})[0] == 401
    assert r.upstream.seen == []
    assert {rec["error"] for rec in r.records()} == {"unauthorized"}
    assert "not-issued" not in (r.ledger_path.read_text())


def test_upstream_gets_the_provider_key_never_the_trial_token(rig):
    r = rig()
    token = r.registry.issue("l", "j", "t", ["model"])
    assert r.post(token.token, {"model": "model", "messages": []})[0] == 200
    assert r.upstream.auth == ["Bearer " + KEY]
    text = r.ledger_path.read_text()
    assert KEY not in text and token.token not in text


def test_nonstream_record_has_spec_fields(rig):
    r = rig()
    token = r.registry.issue("l", "j", "t", ["model"])
    body = {
        "model": "model",
        "temperature": 0.2,
        "tools": [{"type": "function", "function": {"name": "tool_search"}}],
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
    }
    status, data = r.post(token.token, body)
    assert status == 200 and json.loads(data)["model"] == "served-model"
    (rec,) = r.records()
    assert rec["token"] == {"lane": "l", "job": "j", "trial": "t"}
    assert rec["api_family"] == "chat_completions" and rec["role"] == "main"
    assert rec["served_model"] == "served-model" and rec["provider"] == "p"
    assert rec["sampling"] == {"temperature": 0.2}
    assert rec["tools_hash"] and rec["system_hash"] and rec["prompt_hash"]
    assert rec["tool_names"] == ["tool_search"]  # what the model was actually offered
    assert rec["completion"] == "hi" and rec["end_reason"] == "complete"
    assert rec["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "cached_tokens": 4,
        "reasoning_tokens": 2,
        "cost": 0.5,
    }
    assert "request_body" not in rec


def test_stream_relays_incrementally_and_ignores_comments_for_timing(rig):
    def script(handler, body):
        sse(
            handler,
            b": OPENROUTER PROCESSING\n\n",
            0.4,
            event(choices=[{"delta": {"content": "ok"}}], model="m", provider="p"),
            event(choices=[], usage={"prompt_tokens": 2, "completion_tokens": 1}),
            b"data: [DONE]\n\n",
        )

    r = rig(script)
    token = r.registry.issue("l", "j", "t", ["m"])
    conn = http.client.HTTPConnection("127.0.0.1", r.gateway.server_port, timeout=15)
    conn.request(
        "POST",
        "/v1/chat/completions",
        json.dumps({"model": "m", "stream": True}),
        {"Authorization": "Bearer " + token.token},
    )
    response = conn.getresponse()
    assert response.status == 200
    first_line = response.fp.readline()  # the comment arrives before any content
    assert first_line.startswith(b": OPENROUTER")
    assert b'"ok"' in response.read()
    (rec,) = r.records()
    assert rec["stream"] and rec["end_reason"] == "complete"
    assert rec["time_to_first_content"] >= 0.35  # not the ~0 s the comment arrived after
    assert rec["usage"]["prompt_tokens"] == 2
    assert r.upstream.seen[-1]["stream_options"] == {"include_usage": True}


def test_keepalive_only_stream_counts_as_idle(rig):
    def script(handler, body):
        sse(handler, b": OPENROUTER PROCESSING\n\n", b": OPENROUTER PROCESSING\n\n")

    r = rig(script)
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m", "stream": True})[0] == 200
    (rec,) = r.records()
    assert rec["time_to_first_content"] is None and rec["time_of_last_content"] is None
    assert rec["end_reason"] == "truncated_stream"
    assert rec["usage"] == {"unknown": "upstream did not report usage"}


def test_error_event_after_http_200_is_a_failed_request(rig):
    def script(handler, body):
        sse(
            handler,
            event(choices=[{"delta": {"content": "part"}}]),
            event(error={"message": "late", "code": 502}),
            b"data: [DONE]\n\n",
        )

    r = rig(script)
    token = r.registry.issue("l", "j", "t", ["m"])
    status, data = r.post(token.token, {"model": "m", "stream": True})
    assert status == 200 and b"late" in data
    (rec,) = r.records()
    assert rec["end_reason"] == "stream_error" and rec["error"]["message"] == "late"


def test_upstream_http_error_is_relayed_and_recorded(rig):
    def script(handler, body):
        plain(handler, body, status=502, extra={"error": {"message": "down"}})

    r = rig(script)
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m"})[0] == 502
    (rec,) = r.records()
    assert rec["end_reason"] == "http_502" and rec["error"]["message"] == "down"


def test_disallowed_model_is_rejected_recorded_and_flagged(rig):
    r = rig()
    token = r.registry.issue("l", "j", "t", ["model"])
    assert r.post(token.token, {"model": "other"})[0] == 403
    assert r.upstream.seen == []
    (rec,) = r.records()
    assert rec["model"] == "other" and rec["error"] == "disallowed_model"
    assert "disallowed_model" in r.registry.state(token.token)["flags"]


def test_provider_pin_is_injected_per_model(rig):
    r = rig(pins={"model": ["deepinfra"]})
    token = r.registry.issue("l", "j", "t", ["model", "aux"])
    r.post(token.token, {"model": "model", "provider": {"order": ["evil"]}})
    r.post(token.token, {"model": "aux"})
    assert r.upstream.seen[0]["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}
    assert "provider" not in r.upstream.seen[1]


def test_substituted_model_is_flagged_but_alias_is_not(rig):
    r = rig(aliases={"alias": "vendor/model-2025"})

    def script(handler, body):
        plain(handler, body, model=body["_serve"])

    r.upstream.script = script
    token = r.registry.issue("l", "j", "t", ["alias", "model"])
    r.post(token.token, {"model": "alias", "_serve": "vendor/model-2025"})
    r.post(token.token, {"model": "model", "_serve": "cheaper-model"})
    first, second = r.records()
    assert first["model_mismatch"] is False and second["model_mismatch"] is True
    assert r.registry.state(token.token)["flags"] == ["model_mismatch"]


def test_runaway_cap_counts_main_requests_only(rig):
    r = rig()
    token = r.registry.issue("l", "j", "t", ["model", "small"], 2, auxiliary_models=["small"])
    tools = [{"type": "function"}]
    assert r.post(token.token, {"model": "small"})[0] == 200  # auxiliary: not counted
    assert r.post(token.token, {"model": "model", "tools": tools})[0] == 200
    assert r.post(token.token, {"model": "model", "tools": tools})[0] == 200
    assert r.post(token.token, {"model": "model", "tools": tools})[0] == 429
    roles = [rec.get("role") for rec in r.records()]
    assert roles == ["auxiliary", "main", "main", "main"]
    assert r.records()[-1]["error"] == "runaway"
    assert "runaway" in r.registry.state(token.token)["flags"]


def test_request_without_tools_after_tool_requests_is_auxiliary(rig):
    r = rig()
    token = r.registry.issue("l", "j", "t", ["model"])
    r.post(token.token, {"model": "model"})  # before tools are seen: main
    r.post(token.token, {"model": "model", "tools": [{"type": "function"}]})
    r.post(token.token, {"model": "model", "messages": [{"role": "user", "content": "title"}]})
    assert [rec["role"] for rec in r.records()] == ["main", "main", "auxiliary"]
    assert r.registry.state(token.token)["main_requests"] == 2


def test_bodies_recorded_only_when_enabled_and_redacted(rig):
    r = rig(record_bodies=True)
    token = r.registry.issue("l", "j", "t", ["model"])
    r.post(token.token, {"model": "model", "messages": [{"role": "user", "content": KEY}]})
    (rec,) = r.records()
    assert rec["request_body"]["messages"][0]["content"] == "[redacted]"
    assert KEY not in r.ledger_path.read_text()


def test_trial_state_distinguishes_idle_upstream_from_idle_harness(rig):
    release = threading.Event()

    def script(handler, body):
        release.wait(10)
        plain(handler, body)

    r = rig(script)
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.registry.state(token.token)["in_flight"] is False
    thread = threading.Thread(target=r.post, args=(token.token, {"model": "m"}))
    thread.start()
    deadline = time.monotonic() + 5
    while not r.registry.state(token.token)["in_flight"] and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    state = r.registry.state(token.token)
    assert state["in_flight"] is True and state["idle_seconds"] >= 0.15
    release.set()
    thread.join(10)
    state = r.registry.state(token.token)
    assert state["in_flight"] is False and state["idle_seconds"] >= 0


def test_unconfigured_family_named_and_recorded(rig):
    r = rig()
    token = r.registry.issue("l", "j", "t", ["model"])
    status, data = r.post(token.token, {}, "/v1/messages")
    assert status == 400 and b"anthropic_messages" in data
    status, data = r.post(token.token, {}, "/v1/responses")
    assert status == 400 and b"openai_responses" in data
    assert [rec["api_family"] for rec in r.records()] == ["anthropic_messages", "openai_responses"]
    assert r.upstream.seen == []


def test_bind_rejects_wildcard_and_public_addresses():
    for address in ("0.0.0.0", "::", "8.8.8.8"):
        with pytest.raises(ValueError):
            validate_bind_address(address)
    for address in ("127.0.0.1", "172.17.0.1"):
        validate_bind_address(address)


def test_gateway_is_not_reachable_on_other_interfaces(rig):
    r = rig()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; picks the outbound interface
        other = probe.getsockname()[0]
    except OSError:
        pytest.skip("no non-loopback interface")
        return
    finally:
        probe.close()
    if other.startswith("127."):
        pytest.skip("no non-loopback interface")
    with pytest.raises(OSError):
        socket.create_connection((other, r.gateway.server_port), timeout=2)


def test_admin_socket_issue_state_revoke_and_scan(rig, tmp_path):
    r = rig()
    path = str(tmp_path / "admin.sock")
    admin = AdminServer(path, r.registry, r.gateway)
    threading.Thread(target=admin.serve_forever, daemon=True).start()

    def call(**request):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(path)
            sock.sendall(json.dumps(request).encode() + b"\n")
            return json.loads(sock.makefile().readline())

    try:
        issued = call(op="issue", lane="l", job="j", trial="t", allowed_models=["m"])
        assert set(issued) == {"token", "lane", "job", "trial", "model_check"}
        assert r.post(issued["token"], {"model": "m"})[0] == 200
        state = call(op="state", token=issued["token"])
        assert state["trial"] == "t" and state["in_flight"] is False
        assert call(op="revoke", token=issued["token"]) == {"revoked": True}
        assert r.post(issued["token"], {"model": "m"})[0] == 401
        assert "error" in call(op="bogus")
        results = tmp_path / "results"
        results.mkdir()
        (results / "log.txt").write_text("xx" + KEY)
        found = call(op="scan", root=str(results))
        assert found == {"findings": [{"file": str(results / "log.txt"), "offset": 2}]}
        assert KEY not in json.dumps(found)
        assert oct(Path(path).stat().st_mode & 0o777) == "0o660"
    finally:
        admin.shutdown()


def test_scan_reports_file_and_offset_without_the_key(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "clean.txt").write_text("nothing")
    (tmp_path / "a" / "leak.log").write_bytes(b"\x00\x01" + KEY.encode() + b" and " + KEY.encode())
    findings = scan_credentials(tmp_path, KEY)
    assert [f["offset"] for f in findings] == [2, 2 + len(KEY) + 5]
    assert all(f["file"].endswith("leak.log") for f in findings)
    assert KEY not in json.dumps(findings)
    assert scan_credentials(tmp_path, "") == []


GUARDRAIL_404 = {
    "error": {
        "message": "0 endpoints out of 23 requested are available matching your guardrail",
        "code": 404,
        "metadata": {
            "ineligibility_reasons": [
                {"reason": "model-ignored-by-guardrail", "endpoint_count": 23}
            ],
            "failed_routing_step": "Filter by Guardrails",
        },
    }
}


def serve_admin(rig, tmp_path):
    path = str(tmp_path / "admin.sock")
    admin = AdminServer(path, rig.registry, rig.gateway)
    threading.Thread(target=admin.serve_forever, daemon=True).start()

    def call(**request):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(path)
            sock.sendall(json.dumps(request).encode() + b"\n")
            return json.loads(sock.makefile().readline())

    return call, admin


def test_models_op_lists_what_the_guardrail_leaves_the_key(rig, tmp_path):
    r = rig()
    r.upstream.models = ["a/x", "b/y"]
    call, admin = serve_admin(r, tmp_path)
    try:
        listing = call(op="models", check=["a/x", "z/z", "a/x:nitro", "z/z:nitro", "b/y:free"])
        assert listing["models"] == ["a/x", "b/y"]
        assert listing["allowed"] == ["a/x", "a/x:nitro"]
        assert listing["blocked"] == ["z/z", "z/z:nitro", "b/y:free"]
        assert r.upstream.gets == ["/api/v1/models/user"]  # one fetch, then cached
        assert r.upstream.auth[0] == "Bearer " + KEY  # asked as the key, so its guardrail applies
        r.upstream.models = ["c/z"]
        assert call(op="models")["models"] == ["a/x", "b/y"]  # cached
        assert call(op="models", refresh=True)["models"] == ["c/z"]
    finally:
        admin.shutdown()


def test_issue_refuses_a_blocked_model_and_names_it(rig, tmp_path):
    r = rig()
    r.upstream.models = ["a/x", "small/s"]
    call, admin = serve_admin(r, tmp_path)
    try:
        ok = call(op="issue", lane="l", job="j", trial="t", allowed_models=["a/x"])
        assert ok["model_check"] == "ok" and "token" in ok
        blocked = call(op="issue", lane="l", job="j", trial="t2", allowed_models=["openai/gpt-oss"])
        assert "guardrail" in blocked["error"] and "openai/gpt-oss" in blocked["error"]
        aux = call(
            op="issue", lane="l", job="j", trial="t3", allowed_models=["a/x"],
            auxiliary_models=["nope/small"],
        )  # fmt: skip
        assert "nope/small" in aux["error"]
        assert r.registry.state(ok["token"]) is not None and len(r.registry._tokens) == 1
    finally:
        admin.shutdown()


def test_issue_proceeds_when_the_model_list_is_unavailable(rig, tmp_path):
    r = rig()  # upstream.models is None: the list endpoint answers 404
    call, admin = serve_admin(r, tmp_path)
    try:
        issued = call(op="issue", lane="l", job="j", trial="t", allowed_models=["any/model"])
        assert issued["model_check"] == "unavailable" and "token" in issued
        assert "unavailable" in call(op="models")["error"]
    finally:
        admin.shutdown()


def test_guardrail_rejection_is_recorded_flagged_and_refreshes_the_catalog(rig, tmp_path):
    def script(handler, body):
        plain(handler, body, status=404, extra=GUARDRAIL_404)

    r = rig(script)
    r.upstream.models = ["m"]
    call, admin = serve_admin(r, tmp_path)
    try:
        issued = call(op="issue", lane="l", job="j", trial="t", allowed_models=["m"])
        assert r.post(issued["token"], {"model": "m"})[0] == 404
        (rec,) = r.records()
        assert rec["end_reason"] == "guardrail_blocked" and rec["status"] == 404
        assert "guardrail_blocked" in r.registry.state(issued["token"])["flags"]
        r.upstream.models = ["other"]  # the workspace guardrail changed since the list was cached
        again = call(op="issue", lane="l", job="j", trial="t2", allowed_models=["m"])
        assert "guardrail" in again["error"]
    finally:
        admin.shutdown()


def test_other_404s_are_not_mistaken_for_the_guardrail(rig):
    def script(handler, body):
        plain(handler, body, status=404, extra={"error": {"message": "no such model"}})

    r = rig(script)
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m"})[0] == 404
    assert r.records()[0]["end_reason"] == "http_404"
    assert r.registry.state(token.token)["flags"] == []


def flaky(statuses, headers=None):
    """An upstream that answers with the given statuses in turn, then with a normal reply."""
    calls = {"n": 0}

    def script(handler, body):
        i, calls["n"] = calls["n"], calls["n"] + 1
        if i < len(statuses):
            data = json.dumps({"error": {"message": f"upstream {statuses[i]}"}}).encode()
            handler.send_response(statuses[i])
            for k, v in (headers or {}).items():
                handler.send_header(k, v)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        elif body.get("stream"):
            sse(handler, event(choices=[{"delta": {"content": "ok"}}]), b"data: [DONE]\n\n")
        else:
            plain(handler, body)

    script.calls = calls  # type: ignore[attr-defined]
    return script


def test_transient_upstream_errors_are_absorbed_and_recorded(rig):
    slept: list[float] = []
    script = flaky([429, 503])
    r = rig(script, retries=5, sleep=slept.append, retry_base=2.0, retry_cap=30.0)
    token = r.registry.issue("l", "j", "t", ["m"])
    status, data = r.post(token.token, {"model": "m", "messages": []})
    assert (
        status == 200 and json.loads(data)["model"] == "served-model"
    )  # the harness never saw them
    (rec,) = r.records()
    assert rec["attempts"] == 3 and rec["retried"] == [429, 503] and rec["end_reason"] == "complete"
    assert len(slept) == 2 and 1.5 <= slept[0] <= 2.0 and 3.0 <= slept[1] <= 4.0  # exponential
    assert abs(rec["retry_wait"] - sum(slept)) < 1e-9


def test_when_retries_run_out_the_last_error_is_relayed_unchanged(rig):
    r = rig(flaky([503] * 10), retries=2, sleep=lambda s: None)
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m"})[0] == 503
    (rec,) = r.records()
    assert rec["attempts"] == 3 and rec["end_reason"] == "http_503" and rec["retried"] == [503, 503]


def test_errors_that_are_not_transient_are_not_retried(rig):
    for status in (400, 401, 403, 404):
        r = rig(flaky([status] * 5), retries=5, sleep=lambda s: pytest.fail("must not wait"))
        token = r.registry.issue("l", "j", "t", ["m"])
        assert r.post(token.token, {"model": "m"})[0] == status
        assert r.records()[0]["attempts"] == 1


def test_retry_after_is_honoured_and_capped(rig):
    slept: list[float] = []
    r = rig(
        flaky([429], headers={"Retry-After": "7"}),
        retries=3,
        sleep=slept.append,
        retry_base=1.0,
        retry_cap=10.0,
    )
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m"})[0] == 200
    assert 0.75 * 7 <= slept[0] <= 7.0  # not the 1 s exponential default
    slept.clear()
    r2 = rig(
        flaky([429], headers={"Retry-After": "3600"}),
        retries=3,
        sleep=slept.append,
        retry_base=1.0,
        retry_cap=10.0,
    )
    t2 = r2.registry.issue("l", "j", "t", ["m"])
    r2.post(t2.token, {"model": "m"})
    assert slept[0] <= 10.0  # a huge Retry-After cannot stall a trial


def test_the_wait_budget_bounds_retrying(rig):
    slept: list[float] = []
    r = rig(
        flaky([429] * 10), retries=10, sleep=slept.append, retry_base=10.0, retry_cap=30.0,
        retry_budget=15.0,
    )  # fmt: skip
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m"})[0] == 429  # relayed once the budget is spent
    (rec,) = r.records()
    assert rec["attempts"] == len(slept) + 1 and sum(slept) <= 15.0 and len(slept) >= 1


def test_streamed_requests_are_retried_before_the_stream_starts(rig):
    r = rig(flaky([502]), retries=3, sleep=lambda s: None)
    token = r.registry.issue("l", "j", "t", ["m"])
    status, data = r.post(token.token, {"model": "m", "stream": True})
    assert status == 200 and b'"ok"' in data
    (rec,) = r.records()
    assert rec["stream"] and rec["attempts"] == 2 and rec["retried"] == [502]


APP = {
    "HTTP-Referer": "https://example.org/envelope",
    "X-OpenRouter-Title": "Envelope eval",
    "X-OpenRouter-Categories": "cli-agent",
}


def test_the_gateway_identifies_the_app_to_the_provider_on_every_request(rig, tmp_path):
    r = rig(app=APP)
    r.upstream.models = ["m"]
    token = r.registry.issue("l", "j", "t", ["m"])
    assert r.post(token.token, {"model": "m", "messages": []})[0] == 200
    r.gateway.catalog.ids(refresh=True)  # the model-list call is attributed too
    for seen in r.upstream.headers:
        for name, value in APP.items():
            assert seen.get(name) == value, name
    assert len(r.upstream.headers) >= 2


def test_a_harness_cannot_change_the_attribution_and_none_is_sent_by_default(rig):
    r = rig(app=APP)
    token = r.registry.issue("l", "j", "t", ["m"])
    body = json.dumps({"model": "m", "messages": []}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", r.gateway.server_port)
    conn.request(
        "POST",
        "/v1/chat/completions",
        body,
        {
            "Authorization": "Bearer " + token.token,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://evil.example",
            "X-Title": "Not us",
        },
    )
    conn.getresponse().read()
    assert r.upstream.headers[-1]["HTTP-Referer"] == APP["HTTP-Referer"]
    assert "X-Title" not in r.upstream.headers[-1]  # harness headers are not forwarded at all
    plain = rig()
    t2 = plain.registry.issue("l", "j", "t", ["m"])
    plain.post(t2.token, {"model": "m", "messages": []})
    assert "HTTP-Referer" not in plain.upstream.headers[-1]


def test_app_title_without_a_url_is_refused_at_startup(tmp_path):
    import argparse

    from gateway.gateway import app_headers

    ok = argparse.Namespace(app_url="https://x.example", app_title="T", app_categories=None)
    assert app_headers(ok) == {"HTTP-Referer": "https://x.example", "X-OpenRouter-Title": "T"}
    with pytest.raises(SystemExit, match="HTTP-Referer"):
        app_headers(argparse.Namespace(app_url=None, app_title="T", app_categories=None))
    assert app_headers(argparse.Namespace(app_url=None, app_title=None, app_categories=None)) == {}
