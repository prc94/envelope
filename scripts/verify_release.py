"""Smoke-test a built zipapp with isolated Python and real MCP/process lifecycle calls.

Usage: python scripts/verify_release.py dist/envelope.pyz --expected-version 0.1.0rc1
Runs without importing the repository or installing third-party packages.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

CHILD = """\
import json
import os
import sys
import time
from pathlib import Path

Path(sys.argv[2]).write_text(json.dumps({'pid': os.getpid()}), encoding='utf-8')
print('ENVELOPE_RC_READY', flush=True)
if sys.argv[1] == 'background':
    time.sleep(120)
else:
    print('ENVELOPE_RC_DONE', flush=True)
"""


def alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, no mutation
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # PID does not exist
                return False
            raise OSError(error, "Cannot inspect smoke-test process")
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            if status not in (0, 258):
                raise OSError(ctypes.get_last_error(), "Cannot wait on smoke-test process")
            return status == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            text = stat.read_text()
            return text[text.rindex(")") + 2 :].split()[0] != "Z"
        except FileNotFoundError:
            return False
    return True


class Client:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc, self.next_id = proc, 0

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write((json.dumps(request) + "\n").encode())
        await self.proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 30)
            if not line:
                raise RuntimeError("Server closed stdout before responding")
            response = json.loads(line)
            if response.get("id") == self.next_id:
                if "error" in response:
                    raise RuntimeError(str(response["error"]))
                result = response["result"]
                if result.get("isError"):
                    raise RuntimeError(str(result))
                return result

    async def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.call("tools/call", {"name": name, "arguments": arguments})


async def verify(artifact: Path, expected: str, shell: str) -> dict[str, Any]:
    started = time.monotonic()
    with zipfile.ZipFile(artifact) as archive:
        assert b"MIT License" in archive.read("LICENSE")
        assert not any(name.endswith((".pyc", ".pyo")) for name in archive.namelist())
    version = subprocess.run(
        [sys.executable, "-I", "-S", str(artifact), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()
    assert version == f"envelope {expected}", version
    with tempfile.TemporaryDirectory(prefix="envelope-release-") as tmp:
        root = Path(tmp)
        config, child = root / "config.toml", root / "child.py"
        config.write_text("", encoding="utf-8")
        child.write_text(CHILD, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ENVELOPE_")}
        env.update(
            ENVELOPE_SPOOL_DIR=str(root / "spool"), ENVELOPE_AUDIT_LOG=str(root / "audit.jsonl")
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            str(artifact),
            "--config",
            str(config),
            "serve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=root,
        )
        assert proc.stderr is not None
        stderr = asyncio.create_task(proc.stderr.read())
        client = Client(proc)
        background_pids: list[int] = []
        checks = []
        try:
            result = await client.call(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "envelope-release-smoke", "version": "1"},
                },
            )
            assert result["serverInfo"]["version"] == expected
            discovery = await client.call("server/discover", {})
            assert "2026-07-28" in discovery["supportedVersions"]
            tools = (await client.call("tools/list", {}))["tools"]
            assert [tool["name"] for tool in tools] == ["run", "output", "stop", "processes"]
            checks.append("discovery")
            stateless = await client.call(
                "tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
            )
            assert stateless["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] == expected
            assert [tool["name"] for tool in stateless["tools"]] == [
                "run",
                "output",
                "stop",
                "processes",
            ]
            checks.append("stateless_tools")

            def command(mode: str, pid_path: Path) -> str:
                argv = [sys.executable, "-I", "-S", str(child), mode, str(pid_path)]
                if shell == "cmd":
                    return subprocess.list2cmdline(argv)
                if shell in ("pwsh", "powershell"):
                    return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
                if shell == "git-bash":
                    argv = [arg.replace("\\", "/") for arg in argv]
                return shlex.join(argv)

            result = await client.tool(
                "run",
                {
                    "command": command("short", root / "short.json"),
                    "shell": shell,
                    "cwd": str(root),
                    "wait_s": 10,
                },
            )
            assert result["structuredContent"]["exit_code"] == 0
            assert "ENVELOPE_RC_DONE" in result["content"][0]["text"]
            checks.append("command")
            for name in ("stopped", "shutdown"):
                pid_path = root / f"{name}.json"
                result = await client.tool(
                    "run",
                    {
                        "command": command("background", pid_path),
                        "shell": shell,
                        "cwd": str(root),
                        "name": name,
                        "wait_s": 10,
                        "wait_for": "regex:ENVELOPE_RC_READY",
                    },
                )
                assert result["structuredContent"]["state"] == "running"
                pid = int(json.loads(pid_path.read_text())["pid"])
                background_pids.append(pid)
                assert alive(pid)
                listed = (await client.tool("processes", {}))["structuredContent"]["processes"]
                assert any(p["name"] == name and p["state"] == "running" for p in listed)
                output = await client.tool("output", {"handle": name, "tail": 10})
                assert "ENVELOPE_RC_READY" in output["content"][0]["text"]
                if name == "stopped":
                    await client.tool("stop", {"handle": name, "grace_s": 0})
                    assert not alive(pid), "Stopped command is still alive"
                    checks.extend(["background", "processes", "output", "stop"])
        finally:
            assert proc.stdin is not None
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("Server did not exit after stdin closed") from None
            diagnostics = (await stderr).decode("utf-8", "replace")
        assert proc.returncode == 0, diagnostics
        assert all(not alive(pid) for pid in background_pids), "Shutdown left a command alive"
        checks.append("shutdown_cleanup")
    return {
        "version": expected,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "shell": shell,
        "checks": checks,
        "passed": True,
        "duration_s": round(time.monotonic() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--shell", default="cmd" if os.name == "nt" else "bash")
    args = parser.parse_args()
    print(
        json.dumps(asyncio.run(verify(args.artifact.resolve(), args.expected_version, args.shell)))
    )


if __name__ == "__main__":
    main()
