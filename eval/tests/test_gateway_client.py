"""The runner's gateway client against a real gateway, locally and through the relay."""

import io
import json
import sys
import threading
from pathlib import Path

import pytest
from test_gateway_http import KEY, Rig

from envelope_eval.runner.gateway import CommandTransport, GatewayClient
from gateway.gateway import AdminServer, scan_offsets

GATEWAY = Path(__file__).parents[1] / "gateway" / "gateway.py"


@pytest.fixture
def admin(tmp_path):
    rigs = []

    def make():
        from test_gateway_http import plain

        rig = Rig(tmp_path, plain)
        rigs.append(rig)
        path = str(tmp_path / "admin.sock")
        server = AdminServer(path, rig.registry, rig.gateway)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        rigs.append(server)
        return rig, path

    yield make
    for item in rigs:
        (item.close if hasattr(item, "close") else item.shutdown)()


def clients(path):
    """The same client over a local socket and over the relay command."""
    relay = [sys.executable, str(GATEWAY), "relay", "--socket", path]
    return {"unix": GatewayClient.unix(path), "relay": GatewayClient(CommandTransport(relay))}


@pytest.mark.parametrize("via", ["unix", "relay"])
def test_lifecycle_calls_and_ledger_over_either_transport(admin, via):
    rig, path = admin()
    client = clients(path)[via]
    token = client.issue(
        lane="l",
        job="j",
        trial=f"t-{via}",
        allowed_models=["m"],
        request_cap=5,
        auxiliary_models=[],
    )
    assert client.state(token)["in_flight"] is False
    assert rig.post(token, {"model": "m", "messages": []})[0] == 200
    (record,) = client.ledger(f"t-{via}")
    assert record["token"]["trial"] == f"t-{via}" and record["role"] == "main"
    assert client.ledger("someone-else") == []
    client.revoke(token)
    assert rig.post(token, {"model": "m"})[0] == 401


def test_unknown_token_and_bad_operations_raise(admin):
    _, path = admin()
    client = clients(path)["relay"]
    with pytest.raises(RuntimeError, match="unknown token"):
        client.state("nope")


@pytest.mark.parametrize("via", ["unix", "relay"])
def test_scan_streams_the_directory_and_reports_locations_only(admin, tmp_path, via):
    _, path = admin()
    client = clients(path)[via]
    results = tmp_path / "results"
    (results / "sub").mkdir(parents=True)
    (results / "clean.jsonl").write_text("nothing here")
    (results / "sub" / "leak.log").write_bytes(b"xx" + KEY.encode() + b"yy" + KEY.encode())
    findings = client.scan(results)
    assert sorted((f["file"], f["offset"]) for f in findings) == [
        ("./sub/leak.log", 2),
        ("./sub/leak.log", 2 + len(KEY) + 2),
    ]
    assert KEY not in json.dumps(findings)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert client.scan(empty) == []


def test_relay_reports_a_dead_socket(tmp_path):
    client = GatewayClient(
        CommandTransport([sys.executable, str(GATEWAY), "relay", "--socket", str(tmp_path / "x")])
    )
    with pytest.raises(RuntimeError, match="no reply"):
        client.state("t")


def test_scan_offsets_finds_matches_across_chunk_boundaries():
    data = b"..abcabc.abc"
    assert scan_offsets(io.BytesIO(data), b"abc", chunk=4) == [2, 5, 9]
    assert scan_offsets(io.BytesIO(data), b"", chunk=4) == []
    assert scan_offsets(io.BytesIO(b"a"), b"abc", chunk=1) == []


def test_client_from_config_builds_a_pinned_ssh_relay(tmp_path):
    from envelope_eval.runner.gateway import CommandTransport, client_from_config

    config = tmp_path / "gateway.toml"
    config.write_text(
        'host = "gw.lab.lan"\nuser = "gw-runner"\nkey = "/k"\nknown_hosts = "/kh"\nport = 8787\n'
    )
    transport = client_from_config(config).transport
    assert isinstance(transport, CommandTransport)
    argv = transport.argv
    assert argv[0] == "ssh" and "gw-runner@gw.lab.lan" in argv
    assert "StrictHostKeyChecking=yes" in argv and "UserKnownHostsFile=/kh" in argv
    assert "IdentitiesOnly=yes" in argv and "-i" in argv and "/k" in argv


def test_client_from_config_uses_local_socket_on_gateway_host(tmp_path):
    from envelope_eval.runner.gateway import UnixTransport, client_from_config

    config = tmp_path / "gateway.toml"
    config.write_text('socket = "/run/envelope-gateway/admin.sock"\n')
    transport = client_from_config(config).transport
    assert isinstance(transport, UnixTransport)
    assert transport.path == "/run/envelope-gateway/admin.sock"


def test_client_reports_blocked_models_and_lists_the_catalog(admin):
    rig, path = admin()
    rig.upstream.models = ["ok/model"]
    for client in clients(path).values():
        listing = client.models(check=["ok/model", "no/model"])
        assert listing["allowed"] == ["ok/model"] and listing["blocked"] == ["no/model"]
        with pytest.raises(RuntimeError, match="guardrail.*no/model"):
            client.issue(
                lane="l", job="j", trial="t", allowed_models=["no/model"],
                request_cap=1, auxiliary_models=[],
            )  # fmt: skip
