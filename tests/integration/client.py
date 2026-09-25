"""Scripted MCP client that drives a real envelope process over stdio."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


class StdioClient:
    def __init__(
        self,
        tmp_path: Path,
        *,
        config: str | None = None,
        env: dict[str, str] | None = None,
        args: tuple[str, ...] = (),
        cwd: Path | None = None,
        argv: list[str] | None = None,
    ) -> None:
        self.tmp = tmp_path
        self.workdir = cwd or tmp_path / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)
        environment = {
            **os.environ,
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "PYTHONPATH": os.pathsep.join(
                [str(REPO), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
            **(env or {}),
        }
        if config is not None:
            path = tmp_path / "config.toml"
            path.write_text(config, encoding="utf-8")
            environment["ENVELOPE_CONFIG"] = str(path)
        self.proc = subprocess.Popen(
            argv or [sys.executable, "-m", "envelope", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=self.workdir,
        )
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.raw_lines: list[bytes] = []
        self.stderr = b""
        self.notifications: list[dict[str, Any]] = []
        self._responses: dict[Any, dict[str, Any]] = {}
        self._next = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    @property
    def audit_path(self) -> Path:
        return self.tmp / "state" / "envelope" / "audit.jsonl"

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [
            json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.raw_lines.append(line)
            self.messages.put(json.loads(line))
        self.messages.put(None)

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr += line

    def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data if data.endswith(b"\n") else data + b"\n")
        self.proc.stdin.flush()

    def send(self, message: dict[str, Any]) -> None:
        self.send_raw(json.dumps(message).encode())

    def request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._next += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self._next

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def next_message(self, timeout: float = 30) -> dict[str, Any]:
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise EOFError(
                "envelope closed stdout; stderr:\n" + self.stderr.decode(errors="replace")
            )
        return message

    def response(self, rid: int, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while rid not in self._responses:
            message = self.next_message(max(deadline - time.monotonic(), 0.01))
            if "method" in message:
                self.notifications.append(message)
            else:
                self._responses[message.get("id")] = message
        return self._responses.pop(rid)

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60
    ) -> dict[str, Any]:
        return self.response(self.request(method, params), timeout)

    def initialize(self) -> dict[str, Any]:
        result = self.call(
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest"}},
        )
        self.notify("notifications/initialized", {})
        return result

    def tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if meta:
            params["_meta"] = meta
        message = self.call("tools/call", params, timeout)
        assert "result" in message, message
        return message["result"]

    def run(self, command: str, timeout: float = 60, **arguments: Any) -> dict[str, Any]:
        return self.tool("run", {"command": command, **arguments}, timeout=timeout)

    def close(self, timeout: float = 15) -> int:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise


def text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def body_lines(result: dict[str, Any]) -> list[str]:
    return text(result).splitlines()[1:]
