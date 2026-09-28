"""A scripted OpenAI-compatible model, standing in for the provider behind the gateway.

Serves `/api/v1/chat/completions` (streamed or not) and `/api/v1/models/user`. The default
behaviour issues one tool call and then answers: when the harness offers a `tool_call` wrapper
(Qwen Code defers MCP tools), it calls envelope's `run` through it; when it offers
`run_shell_command`, it calls that; after a tool result it answers with `answer`.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Plan = Callable[[dict[str, Any]], dict[str, Any]]


def default_plan(command: str, answer: str, shell: str | None = None) -> Plan:
    def plan(body: dict[str, Any]) -> dict[str, Any]:
        messages = body.get("messages") or []
        offered = {t["function"]["name"] for t in body.get("tools") or []}
        if messages and messages[-1].get("role") == "tool":
            return {"text": answer}
        args: dict[str, Any] = {"command": command}
        if shell:
            args["shell"] = shell
        if "tool_call" in offered:
            wrapped = {"name": "mcp__envelope__run", "arguments": args}
            return {"tool": "tool_call", "arguments": wrapped}
        if "run_shell_command" in offered:
            return {
                "tool": "run_shell_command",
                "arguments": {"command": command, "description": "run", "is_background": False},
            }
        return {"text": answer}

    return plan


class FakeModel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, plan: Plan, models: list[str], port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.plan, self.models = plan, models
        self.requests: list[dict[str, Any]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        fake: FakeModel = self.server  # type: ignore[assignment]
        data = json.dumps({"data": [{"id": m} for m in fake.models]}).encode()
        self.send_response(200 if self.path.endswith("/models/user") else 404)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        fake: FakeModel = self.server  # type: ignore[assignment]
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        fake.requests.append(body)
        step = fake.plan(body)
        if "tool" in step:
            delta: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(fake.requests)}",
                        "type": "function",
                        "function": {
                            "name": step["tool"],
                            "arguments": json.dumps(step["arguments"]),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": step["text"]}, "stop"
        usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        model = body.get("model", "fake")
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in (
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                {"choices": [], "usage": usage},
            ):
                chunk = {"id": "c", "object": "chat.completion.chunk", "model": model, **event}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            data = json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "model": model,
                    "usage": usage,
                    "choices": [{"index": 0, "message": delta, "finish_reason": finish}],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
