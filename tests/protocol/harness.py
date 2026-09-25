"""In-memory MCP client harness for driving ``Server`` without subprocesses."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from envelope.protocol.server import Server
from envelope.protocol.transport import QueueLineSource


class Harness:
    def __init__(self, server: Server) -> None:
        self.server = server
        self.source = QueueLineSource()
        self.raw: list[bytes] = []
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._next_id = 0

    def _write(self, data: bytes) -> None:
        assert data.endswith(b"\n") and data.count(b"\n") == 1
        self.raw.append(data)
        self.messages.put_nowait(json.loads(data))

    async def __aenter__(self) -> Harness:
        self._task = asyncio.create_task(self.server.serve(self.source, self._write))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.source.feed(None)
        if self._task is not None:
            await asyncio.wait_for(self._task, 30)
            self._task = None

    def send_raw(self, line: bytes) -> None:
        self.source.feed(line)

    def send(self, message: dict[str, Any]) -> None:
        self.source.feed(json.dumps(message).encode())

    def request(self, method: str, params: dict[str, Any] | None = None, *, rid: Any = None) -> Any:
        if rid is None:
            self._next_id += 1
            rid = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return rid

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    async def next(self, timeout: float = 10) -> dict[str, Any]:
        return await asyncio.wait_for(self.messages.get(), timeout)

    async def response(self, rid: Any, timeout: float = 30) -> dict[str, Any]:
        """Next message with the given id; notifications seen meanwhile are kept in ``seen``."""
        while True:
            message = await self.next(timeout)
            if message.get("id") == rid and "method" not in message:
                return message
            self.seen.append(message)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.response(self.request(method, params))

    @property
    def seen(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_seen"):
            self._seen: list[dict[str, Any]] = []
        return self._seen
