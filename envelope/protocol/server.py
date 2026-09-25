"""MCP server over stdio supporting protocol revisions 2026-07-28 and 2025-11-25."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from envelope.log import logger
from envelope.protocol import jsonrpc
from envelope.protocol.jsonrpc import RequestId, RpcError
from envelope.protocol.transport import LineSource, Writer

STATELESS = "2026-07-28"
LEGACY = "2025-11-25"
SUPPORTED_VERSIONS: tuple[str, ...] = (STATELESS, LEGACY)

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

TOOLS_TTL_MS = 3_600_000
DISCOVER_TTL_MS = 3_600_000


@dataclass
class ToolResult:
    text: str
    structured: dict[str, Any] | None = None
    is_error: bool = False

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": self.text}]}
        if self.structured is not None:
            result["structuredContent"] = self.structured
        if self.is_error:
            result["isError"] = True
        return result


@dataclass
class CallContext:
    request_id: RequestId
    protocol_version: str
    client_info: dict[str, Any] | None
    client_capabilities: dict[str, Any] | None
    has_progress: bool = False
    status: Callable[[], str] | None = None
    """Set by the tool: returns a short progress message (elapsed, output size, last line)."""
    extra: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


class Tool(Protocol):
    name: str

    def definition(self) -> dict[str, Any]: ...

    async def call(self, arguments: dict[str, Any], ctx: CallContext) -> ToolResult: ...


def _key(request_id: RequestId) -> str:
    return json.dumps(request_id)


class Server:
    def __init__(
        self,
        *,
        tools: Sequence[Tool],
        server_info: dict[str, Any],
        instructions: str,
        progress_interval_s: float = 5.0,
        on_shutdown: Callable[[], Any] | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._tool_order = [tool.name for tool in tools]
        self._server_info = server_info
        self._instructions = instructions
        self._progress_interval = progress_interval_s
        self._negotiated: str | None = None
        self._client_info: dict[str, Any] | None = None
        self._client_caps: dict[str, Any] | None = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._write: Writer = lambda _data: None
        self._on_shutdown = on_shutdown

    # --- transport loop ------------------------------------------------------

    async def serve(self, source: LineSource, write: Writer) -> None:
        self._write = write
        tasks: set[asyncio.Task[None]] = set()
        while True:
            line = await source.readline()
            if line is None:
                break
            if not line.strip():
                continue
            task = asyncio.create_task(self._handle_line(line))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            await asyncio.sleep(0)  # let the handler register itself before the next line
        logger.debug("stdin closed; cancelling %d in-flight request(s)", len(self._inflight))
        for key, task in list(self._inflight.items()):
            self._cancelled.add(key)
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._on_shutdown is not None:
            result = self._on_shutdown()
            if asyncio.iscoroutine(result):
                await result

    def _send(self, message: dict[str, Any]) -> None:
        data = jsonrpc.encode(message)
        if "id" in message:
            logger.debug(
                "-> %s id=%s (%d bytes)",
                "error" if "error" in message else "result",
                message["id"],
                len(data),
            )
        self._write(data)

    async def _handle_line(self, line: bytes) -> None:
        try:
            message = jsonrpc.decode(line)
        except RpcError as exc:
            self._send(jsonrpc.error_response(None, exc))
            return
        if not isinstance(message, dict):
            what = (
                "batch requests are not supported" if isinstance(message, list) else "not an object"
            )
            self._send(
                jsonrpc.error_response(
                    None, RpcError(jsonrpc.INVALID_REQUEST, f"Invalid Request: {what}")
                )
            )
            return
        await self._handle_message(cast(dict[str, Any], message))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        has_id = "id" in message
        request_id = message.get("id")
        if method is None and ("result" in message or "error" in message):
            return  # a response to a request we never send; ignore
        if (
            message.get("jsonrpc") != "2.0"
            or not isinstance(method, str)
            or (has_id and not jsonrpc.valid_id(request_id))
        ):
            if has_id or method is None:
                rid = request_id if jsonrpc.valid_id(request_id) else None
                error = RpcError(jsonrpc.INVALID_REQUEST, "Invalid Request")
                self._send(jsonrpc.error_response(rid, error))
            return
        params_raw = message.get("params", {})
        if not isinstance(params_raw, dict):
            if has_id:
                error = RpcError(jsonrpc.INVALID_PARAMS, "params must be an object")
                self._send(jsonrpc.error_response(request_id, error))
            return
        params = cast(dict[str, Any], params_raw)
        logger.debug("<- %s%s", method, f" id={request_id}" if has_id else "")
        if not has_id:
            self._handle_notification(method, params)
            return
        rid = cast(RequestId, request_id)
        key = _key(rid)
        task = asyncio.current_task()
        if task is not None:
            self._inflight[key] = task
        try:
            result = await self._dispatch(method, params, rid)
        except asyncio.CancelledError:
            if key in self._cancelled:
                logger.debug("request %s cancelled; no response sent", rid)
                return
            raise
        except RpcError as exc:
            self._send(jsonrpc.error_response(rid, exc))
            return
        except Exception as exc:
            logger.exception("internal error handling %s", method)
            error = RpcError(jsonrpc.INTERNAL_ERROR, f"Internal error: {exc}")
            self._send(jsonrpc.error_response(rid, error))
            return
        finally:
            self._inflight.pop(key, None)
            self._cancelled.discard(key)
        self._send(jsonrpc.response(rid, result))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/cancelled":
            request_id = params.get("requestId")
            if not jsonrpc.valid_id(request_id):
                return
            key = _key(cast(RequestId, request_id))
            task = self._inflight.get(key)
            if task is not None:
                logger.debug("cancelling request %s: %s", request_id, params.get("reason", ""))
                self._cancelled.add(key)
                task.cancel()
        # notifications/initialized and unknown notifications need no action.

    # --- version handling ----------------------------------------------------

    def _resolve_version(self, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        meta_raw = params.get("_meta")
        meta = cast(dict[str, Any], meta_raw) if isinstance(meta_raw, dict) else {}
        declared = meta.get(META_VERSION)
        if declared is not None:
            if declared not in SUPPORTED_VERSIONS:
                raise RpcError(
                    jsonrpc.UNSUPPORTED_PROTOCOL_VERSION,
                    "Unsupported protocol version",
                    {"supported": list(SUPPORTED_VERSIONS), "requested": declared},
                )
            return cast(str, declared), meta
        return self._negotiated or LEGACY, meta

    def _finish(self, result: dict[str, Any], version: str) -> dict[str, Any]:
        result["resultType"] = "complete"
        if version == STATELESS:
            meta_raw = result.get("_meta")
            meta = cast(dict[str, Any], meta_raw) if isinstance(meta_raw, dict) else {}
            meta[META_SERVER_INFO] = self._server_info
            result["_meta"] = meta
        return result

    def _capabilities(self) -> dict[str, Any]:
        return {"tools": {"listChanged": False}}

    # --- methods -------------------------------------------------------------

    async def _dispatch(
        self, method: str, params: dict[str, Any], rid: RequestId
    ) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "server/discover":
            _version, meta = self._resolve_version(params)
            return self._finish(
                {
                    "supportedVersions": list(SUPPORTED_VERSIONS),
                    "capabilities": self._capabilities(),
                    "instructions": self._instructions,
                    "ttlMs": DISCOVER_TTL_MS,
                    "cacheScope": "private",
                },
                STATELESS if meta.get(META_VERSION) is None else cast(str, meta[META_VERSION]),
            )
        version, meta = self._resolve_version(params)
        if method == "ping":
            return self._finish({}, version)
        if method == "tools/list":
            result: dict[str, Any] = {
                "tools": [self._tools[n].definition() for n in self._tool_order]
            }
            if version == STATELESS:
                result["ttlMs"] = TOOLS_TTL_MS
                result["cacheScope"] = "private"
            return self._finish(result, version)
        if method == "tools/call":
            return self._finish(await self._call_tool(params, meta, version, rid), version)
        raise RpcError(jsonrpc.METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        # 2026-07-28 has no initialize; anyone using the handshake gets 2025-11-25.
        self._negotiated = LEGACY
        client_info = params.get("clientInfo")
        caps = params.get("capabilities")
        self._client_info = (
            cast(dict[str, Any], client_info) if isinstance(client_info, dict) else None
        )
        self._client_caps = cast(dict[str, Any], caps) if isinstance(caps, dict) else None
        logger.debug("initialize: client=%s requested=%s", self._client_info, requested)
        return self._finish(
            {
                "protocolVersion": LEGACY,
                "capabilities": self._capabilities(),
                "serverInfo": self._server_info,
                "instructions": self._instructions,
            },
            LEGACY,
        )

    async def _call_tool(
        self, params: dict[str, Any], meta: dict[str, Any], version: str, rid: RequestId
    ) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(jsonrpc.INVALID_PARAMS, "tools/call requires a string 'name'")
        tool = self._tools.get(name)
        if tool is None:
            raise RpcError(
                jsonrpc.INVALID_PARAMS,
                f"Unknown tool: {name}",
                {"available": list(self._tool_order)},
            )
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(jsonrpc.INVALID_PARAMS, "tools/call 'arguments' must be an object")
        client_info = meta.get(META_CLIENT_INFO, self._client_info)
        caps = meta.get(META_CLIENT_CAPS, self._client_caps)
        token = meta.get("progressToken")
        ctx = CallContext(
            request_id=rid,
            protocol_version=version,
            client_info=cast(dict[str, Any], client_info)
            if isinstance(client_info, dict)
            else None,
            client_capabilities=cast(dict[str, Any], caps) if isinstance(caps, dict) else None,
            has_progress=jsonrpc.valid_id(token),
        )
        heartbeat = (
            asyncio.create_task(self._heartbeat(cast(RequestId, token), ctx))
            if ctx.has_progress
            else None
        )
        try:
            result = await tool.call(cast(dict[str, Any], arguments), ctx)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
        return result.to_json()

    async def _heartbeat(self, token: RequestId, ctx: CallContext) -> None:
        start = time.monotonic()
        last = 0.0
        while True:
            await asyncio.sleep(self._progress_interval)
            elapsed = time.monotonic() - start
            progress = max(round(elapsed, 1), last + 0.1)
            last = progress
            message = ctx.status() if ctx.status is not None else f"running {elapsed:.0f}s"
            self._send(
                jsonrpc.notification(
                    "notifications/progress",
                    {"progressToken": token, "progress": progress, "message": message},
                )
            )
