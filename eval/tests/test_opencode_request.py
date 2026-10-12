"""Check task transport through the real CLI against a local model, without API spend."""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from envelope_eval.adapters.opencode import OpenCodeAdapter

pytestmark = pytest.mark.integration
OPENCODE = shutil.which("opencode")


@pytest.mark.skipif(OPENCODE is None, reason="OpenCode binary is not installed")
@pytest.mark.parametrize("names", [("run", "output"), ("run", "output", "stop", "processes")])
@pytest.mark.parametrize("prompt", [None, "Complete the user's task with the available tools."])
def test_real_cli_preserves_the_instruction_and_offers_the_configured_tools(
    tmp_path, names, prompt
):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            reply = {
                "id": "capture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Captured."},
                        "finish_reason": None,
                    }
                ],
            }
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"data: {json.dumps(reply)}\n\n".encode())
                reply["choices"][0].update(delta={}, finish_reason="stop")
                self.wfile.write(f"data: {json.dumps(reply)}\n\ndata: [DONE]\n\n".encode())
            else:
                reply["object"] = "chat.completion"
                choice = reply["choices"][0]
                choice["message"] = choice.pop("delta")
                choice["finish_reason"] = "stop"
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())

    repo = Path(__file__).resolve().parents[2]
    tools = [f"mcp__envelope__{name}" for name in names]
    adapter = OpenCodeAdapter(
        launch=[str(OPENCODE)],
        envelope_command=[sys.executable, "-m", "envelope"],
        model_prompts={"qwen/qwen3.8-flash": prompt} if prompt else None,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Leading flags, quotes, multiline code and repeated spaces must arrive as task text.
    instruction = '  --help\nRun printf "a  b\\n"; sleep 40; seq 1 20000; then say `готово`.\n\tKeep spaces.  '
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        adapter.configure(
            tmp_path,
            "qwen/qwen3.8-flash",
            "capture-only",
            tools,
            base_url,
            {"PYTHONPATH": str(repo)},
        )
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / ".config"),
            "XDG_DATA_HOME": str(tmp_path / ".local" / "share"),
            "XDG_CACHE_HOME": str(tmp_path / ".cache"),
            "XDG_STATE_HOME": str(tmp_path / ".local" / "state"),
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            **adapter.environment("qwen/qwen3.8-flash", "capture-only", base_url),
        }
        try:
            result = subprocess.run(
                adapter.command(instruction, workspace, "envelope", tools),
                env=env,
                input=adapter.stdin(instruction),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.returncode == 0, result.stderr
    main = [request for request in requests if request.get("tools")]
    assert main, result.stdout
    for request in main:
        assert request["messages"][-1]["content"] == instruction
        if prompt:
            system = request["messages"][0]["content"]
            assert system.startswith(prompt)
            assert "<mcp_instructions>" in system
            assert "<env>" in system
        assert {tool["function"]["name"] for tool in request["tools"]} == {
            f"envelope_{name}" for name in names
        }
