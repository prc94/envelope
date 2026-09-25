from __future__ import annotations

import asyncio
from typing import Any

from envelope.protocol.server import (
    META_SERVER_INFO,
    META_VERSION,
    CallContext,
    Server,
    ToolResult,
)
from tests.protocol.harness import Harness

INFO = {"name": "envelope", "version": "0.0-test"}


class EchoTool:
    name = "echo"

    def __init__(self) -> None:
        self.cleaned_up = asyncio.Event()
        self.started = asyncio.Event()
        self.contexts: list[CallContext] = []

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": "echo",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        }

    async def call(self, arguments: dict[str, Any], ctx: CallContext) -> ToolResult:
        self.contexts.append(ctx)
        if arguments.get("fail"):
            return ToolResult("bad argument", {"error": "x"}, is_error=True)
        sleep = float(arguments.get("sleep", 0))
        if sleep:
            ctx.status = lambda: "working"
            self.started.set()
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                self.cleaned_up.set()
                raise
        return ToolResult(str(arguments.get("text", "")), {"ok": True})


def make_server(tool: EchoTool | None = None, interval: float = 5.0) -> Server:
    return Server(
        tools=[tool or EchoTool()],
        server_info=INFO,
        instructions="test instructions",
        progress_interval_s=interval,
    )


def meta(version: str = "2026-07-28", **extra: Any) -> dict[str, Any]:
    return {"_meta": {META_VERSION: version, **extra}}


async def test_malformed_input_returns_jsonrpc_errors() -> None:
    async with Harness(make_server()) as h:
        h.send_raw(b"{not json")
        msg = await h.next()
        assert msg["error"]["code"] == -32700 and msg["id"] is None
        h.send_raw(b"[1, 2]")
        assert (await h.next())["error"]["code"] == -32600
        h.send({"jsonrpc": "1.0", "id": 5, "method": "ping"})
        msg = await h.next()
        assert msg["error"]["code"] == -32600 and msg["id"] == 5
        h.send({"jsonrpc": "2.0", "id": 6, "method": "ping", "params": [1]})
        assert (await h.next())["error"]["code"] == -32602
        msg = await h.call("no/such/method")
        assert msg["error"]["code"] == -32601
        # Unknown notifications are ignored; the server keeps serving.
        h.notify("notifications/whatever")
        assert "result" in await h.call("ping")


async def test_legacy_handshake() -> None:
    async with Harness(make_server()) as h:
        msg = await h.call(
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "c"}},
        )
        result = msg["result"]
        assert result["protocolVersion"] == "2025-11-25"
        assert result["serverInfo"] == INFO
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["instructions"] == "test instructions"
        h.notify("notifications/initialized")
        tools = (await h.call("tools/list"))["result"]["tools"]
        assert [t["name"] for t in tools] == ["echo"]
        assert "ttlMs" not in (await h.call("tools/list"))["result"]
        call = await h.call("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert call["result"]["content"] == [{"type": "text", "text": "hi"}]
        assert "isError" not in call["result"]


async def test_stateless_call_without_handshake() -> None:
    tool = EchoTool()
    async with Harness(make_server(tool)) as h:
        params: dict[str, Any] = {"name": "echo", "arguments": {"text": "x"}, **meta()}
        params["_meta"]["io.modelcontextprotocol/clientInfo"] = {"name": "qwen"}
        msg = await h.call("tools/call", params)
        result = msg["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"][META_SERVER_INFO] == INFO
        assert tool.contexts[0].protocol_version == "2026-07-28"
        assert tool.contexts[0].client_info == {"name": "qwen"}


async def test_discovery() -> None:
    async with Harness(make_server()) as h:
        result = (await h.call("server/discover", meta()))["result"]
        assert result["supportedVersions"] == ["2026-07-28", "2025-11-25"]
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["instructions"] == "test instructions"
        assert result["_meta"][META_SERVER_INFO] == INFO
        assert result["resultType"] == "complete"
        assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
        assert result["cacheScope"] in ("public", "private")


async def test_unsupported_version() -> None:
    async with Harness(make_server()) as h:
        msg = await h.call("tools/list", meta("2023-01-01"))
        error = msg["error"]
        assert error["code"] == -32022
        assert error["data"]["supported"] == ["2026-07-28", "2025-11-25"]
        assert error["data"]["requested"] == "2023-01-01"


async def test_tools_list_is_deterministic_and_cacheable() -> None:
    async with Harness(make_server()) as h:
        first = await h.call("tools/list", meta())
        second = await h.call("tools/list", meta())
        assert h.raw[0].replace(b'"id":1', b'"id":2') == h.raw[1]
        assert first["result"]["tools"] == second["result"]["tools"]
        assert first["result"]["ttlMs"] > 0
        assert first["result"]["cacheScope"] == "private"


async def test_tool_call_error_shapes() -> None:
    async with Harness(make_server()) as h:
        unknown = await h.call("tools/call", {"name": "nope", "arguments": {}})
        assert unknown["error"]["code"] == -32602
        assert "Unknown tool" in unknown["error"]["message"]
        bad_args = await h.call("tools/call", {"name": "echo", "arguments": "text"})
        assert bad_args["error"]["code"] == -32602
        tool_error = await h.call("tools/call", {"name": "echo", "arguments": {"fail": True}})
        assert tool_error["result"]["isError"] is True
        assert tool_error["result"]["content"][0]["type"] == "text"


async def test_progress_only_with_token() -> None:
    async with Harness(make_server(interval=0.2)) as h:
        with_token = await h.call(
            "tools/call",
            {"name": "echo", "arguments": {"sleep": 0.9}, "_meta": {"progressToken": "p1"}},
        )
        assert "result" in with_token
        progress = [m for m in h.seen if m.get("method") == "notifications/progress"]
        assert len(progress) >= 3
        values = [m["params"]["progress"] for m in progress]
        assert values == sorted(values) and len(set(values)) == len(values)
        assert all(m["params"]["progressToken"] == "p1" for m in progress)
        assert progress[0]["params"]["message"] == "working"
        h.seen.clear()
        await h.call("tools/call", {"name": "echo", "arguments": {"sleep": 0.6}})
        assert not [m for m in h.seen if m.get("method") == "notifications/progress"]


async def test_cancellation_suppresses_response() -> None:
    tool = EchoTool()
    async with Harness(make_server(tool)) as h:
        rid = h.request("tools/call", {"name": "echo", "arguments": {"sleep": 60}})
        await asyncio.wait_for(tool.started.wait(), 5)
        h.notify("notifications/cancelled", {"requestId": rid, "reason": "user abort"})
        await asyncio.wait_for(tool.cleaned_up.wait(), 5)
        # The next response must belong to the ping, not the cancelled call.
        ping = await h.call("ping")
        assert ping["id"] != rid
        assert all(m.get("id") != rid for m in h.seen)


async def test_stdin_eof_cancels_inflight_calls() -> None:
    tool = EchoTool()
    h = Harness(make_server(tool))
    await h.__aenter__()
    h.request("tools/call", {"name": "echo", "arguments": {"sleep": 60}})
    await asyncio.wait_for(tool.started.wait(), 5)
    await h.close()
    assert tool.cleaned_up.is_set()
    assert h.messages.empty()
