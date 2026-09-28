#!/usr/bin/env python3
"""Model-free stand-in harness: run a task's oracle through envelope's MCP server. Standard
library only; runs on the target inside the containment unit, exactly where a real harness
would.

    python oracle.py --envelope ENVELOPE.pyz (--oracle FILE | --oracle-in-home NAME)
                     [--instruction TEXT] [--shell ID] [--cwd DIR]

Starts `python ENVELOPE.pyz` (stdio MCP), calls `run` with the oracle file's contents as the
command, and writes a transcript in Qwen Code's `stream-json` shape on stdout: an init event
listing the two tools, the tool call, its result, and the result text as the final answer. The
rest of the pipeline (parser, tool-set check, collect, judge, integrity, audit log) then runs
unchanged, so the oracle exercises the same path a model trial does.

The environment (ENVELOPE_AUDIT_LOG, ENVELOPE_SPOOL_DIR, HOME, ...) is inherited from the
launcher, so envelope writes its audit log and spool into the trial's directories.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOLS = ["mcp__envelope__run", "mcp__envelope__output"]


def emit(event: dict[str, Any]) -> None:
    sys.stdout.buffer.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


class Server:
    def __init__(self, pyz: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, pyz],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._id = 0

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("envelope closed its output")
            reply = json.loads(line)
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def code_block(instruction: str) -> str:
    """The first fenced code block of the instruction: tasks state literal commands there."""
    lines, inside, block = instruction.splitlines(), False, []
    for line in lines:
        if line.strip().startswith("```"):
            if inside:
                break
            inside = True
        elif inside:
            block.append(line)
    if not block:
        raise RuntimeError("the instruction has no fenced code block for {{CODE}}")
    return "\n".join(block)


def load_steps(text: str, instruction: str) -> list[dict[str, Any]]:
    """An oracle is a plain command (run as is), or a JSON list of steps
    `{"tool": "run"|"output", "arguments": {...}}`. `{{CODE}}` stands for the instruction's first
    code block and `$handle` for the output handle of the previous step."""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    steps = (
        parsed if isinstance(parsed, list) else [{"tool": "run", "arguments": {"command": text}}]
    )
    if "{{CODE}}" in text:
        code = code_block(instruction)
        steps = json.loads(json.dumps(steps).replace("{{CODE}}", json.dumps(code)[1:-1]))
    return steps


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--oracle", help="file holding the oracle")
    parser.add_argument("--oracle-in-home", help="name of the oracle file in the trial's home")
    parser.add_argument("--instruction", default="", help="the rendered instruction text")
    parser.add_argument("--shell")
    parser.add_argument("--cwd")
    args = parser.parse_args(argv)
    if bool(args.oracle) == bool(args.oracle_in_home):
        parser.error("give exactly one of --oracle and --oracle-in-home")
    oracle = Path(args.oracle) if args.oracle else Path.home() / args.oracle_in_home
    steps = load_steps(oracle.read_text(encoding="utf-8"), args.instruction)

    server = Server(args.envelope)
    try:
        server.call(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "eval-oracle", "version": "1"},
            },
        )
        server.notify("notifications/initialized")
        names = {t["name"] for t in server.call("tools/list")["tools"]}
        emit({"type": "system", "subtype": "init", "tools": TOOLS, "mcp_servers": []})
        if not {"run", "output"} <= names:
            raise RuntimeError(f"envelope exposes {sorted(names)}, expected run and output")
        handle, text = None, ""
        for number, step in enumerate(steps, 1):
            arguments = dict(step["arguments"])
            if step["tool"] == "run":
                if args.shell:
                    arguments.setdefault("shell", args.shell)
                if args.cwd:
                    arguments.setdefault("cwd", args.cwd)
            elif "handle" in arguments:
                arguments["handle"] = str(arguments["handle"]).replace("$handle", str(handle))
            call_id = f"oracle_{number}"
            emit(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": f"mcp__envelope__{step['tool']}",
                                "input": arguments,
                            }
                        ],
                    },
                }
            )
            result = server.call("tools/call", {"name": step["tool"], "arguments": arguments})
            text = "\n".join(c.get("text", "") for c in result.get("content", []))
            handle = (result.get("structuredContent") or {}).get("handle", handle)
            emit(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "is_error": bool(result.get("isError")),
                                "content": text,
                            }
                        ],
                    },
                }
            )
        # A model's answer never contains envelope's `exit N | 1.2s | shell | cwd ...` header.
        body = text.split("\n", 1)[1] if text.startswith("exit ") and "\n" in text else text
        answer = body.strip()
        emit(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]},
            }
        )
        emit({"type": "result", "subtype": "success", "is_error": False, "result": answer})
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
