"""Model-driven interop runs: OpenCode and Qwen Code (via OpenRouter) against envelope.pyz.

Each harness gets one fixed project directory (so Qwen's per-project MCP approval is
needed only once) with envelope registered as an MCP server and the harness's own
shell tool disabled. Scenarios are verified from envelope's audit log plus the
harness's final answer.

Usage:
  python scripts/interop.py [--harness opencode qwen] [--model ID ...] [--scenario NAME ...]
  python scripts/interop.py --setup-only      # write configs, print approval hint
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYZ = ROOT / "dist" / "envelope.pyz"
BASE = Path(os.environ.get("ENVELOPE_INTEROP_DIR", "/tmp/envelope-interop"))
DEFAULT_MODELS = [
    "deepseek/deepseek-v4-flash-0731",
    "qwen/qwen3.8-flash",
    "z-ai/glm-5.3-flash",
]


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    expect: str
    needs_output_tool: bool = False
    min_duration_ms: int = 0
    timeout_s: int = 600


SCENARIOS = [
    Scenario(
        "basic",
        "Use the envelope `run` tool (do not use any other tool) to execute exactly this "
        "command: echo interop-$((6*7))\nThen reply with only the command's output.",
        "interop-42",
    ),
    Scenario(
        "truncation",
        "Use the envelope `run` tool to execute exactly this command:\n"
        'seq 1 20000 | awk \'{print "row " $1} NR==12345 {print "TOKEN=" 7*11*13*17}\'\n'
        "The output is long and will be shortened. One line starts with TOKEN= . Find it "
        "without re-running the command and reply with only the value after TOKEN=.",
        "17017",
        needs_output_tool=True,
    ),
    Scenario(
        "long",
        "Use the envelope `run` tool to execute exactly this command (it takes about 40 "
        "seconds; wait for it): sleep 40; echo finished-$((2+3))\n"
        "Then reply with only the command's output.",
        "finished-5",
        min_duration_ms=39_000,
        timeout_s=700,
    ),
]


def python_exe() -> str:
    return os.environ.get("ENVELOPE_INTEROP_PYTHON") or shutil.which("python3.12") or sys.executable


def envelope_env(project: Path) -> dict[str, str]:
    return {
        "ENVELOPE_AUDIT_LOG": str(project / "audit.jsonl"),
        "ENVELOPE_SPOOL_DIR": str(project / "spool"),
        "ENVELOPE_DEFAULT_SHELL": "bash",
        "ENVELOPE_DEFAULT_CWD": str(project),
    }


def setup_opencode(models: list[str]) -> Path:
    project = BASE / "opencode"
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(project)], check=True)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "envelope": {
                "type": "local",
                "command": [python_exe(), str(PYZ)],
                "enabled": True,
                "environment": envelope_env(project),
            }
        },
        "tools": {"bash": False, "write": False, "edit": False},
        "permission": {"bash": "deny", "edit": "deny"},
    }
    (project / "opencode.json").write_text(json.dumps(config, indent=2))
    return project


def setup_qwen(models: list[str]) -> Path:
    project = BASE / "qwen"
    (project / ".qwen").mkdir(parents=True, exist_ok=True)
    if not (project / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(project)], check=True)
    settings = {
        "mcpServers": {
            "envelope": {"command": python_exe(), "args": [str(PYZ)], "env": envelope_env(project)}
        },
        "tools": {"exclude": ["run_shell_command"]},
    }
    (project / ".qwen" / "settings.json").write_text(json.dumps(settings, indent=2))
    return project


def openrouter_key() -> str:
    """OPENROUTER_API_KEY from the environment, else from Qwen Code's user settings."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    settings = json.loads((Path.home() / ".qwen" / "settings.json").read_text())
    return str(settings.get("env", {}).get("OPENROUTER_API_KEY", ""))


def audit_since(project: Path, offset: int) -> list[dict[str, Any]]:
    path = project / "audit.jsonl"
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        return [json.loads(line) for line in handle.read().decode().splitlines() if line.strip()]


def audit_size(project: Path) -> int:
    path = project / "audit.jsonl"
    return path.stat().st_size if path.exists() else 0


def final_text_opencode(stdout: str) -> str:
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") or {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return texts[-1] if texts else ""


def final_text_qwen(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip().splitlines()[-1] if stdout.strip() else ""
    items = data if isinstance(data, list) else [data]
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "result":
            return str(item.get("result", ""))
    return ""


def run_one(harness: str, project: Path, model: str, scenario: Scenario) -> dict[str, Any]:
    if harness == "opencode":
        # OpenCode's title agent otherwise picks its own "small model" (on OpenRouter:
        # google/gemini-*-flash); pin it to the model under test.
        config_path = project / "opencode.json"
        config = json.loads(config_path.read_text())
        config["small_model"] = f"openrouter/{model}"
        config_path.write_text(json.dumps(config, indent=2))
        argv = [
            "opencode",
            "run",
            "--pure",
            "--format",
            "json",
            "--dir",
            str(project),
            "-m",
            f"openrouter/{model}",
            scenario.prompt,
        ]
    else:
        # Selected via OPENAI_* env: Qwen Code does not match a `qwen/...` id against
        # settings.modelProviders and silently falls back to another configured model.
        argv = [
            "qwen",
            "--auth-type",
            "openai",
            "-m",
            model,
            "--approval-mode",
            "yolo",
            "-o",
            "json",
            scenario.prompt,
        ]
    offset = audit_size(project)
    started = time.monotonic()
    env = {**os.environ, "PWD": str(project), "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1"}
    if harness == "qwen":
        env.update(
            OPENAI_API_KEY=openrouter_key(),
            OPENAI_BASE_URL="https://openrouter.ai/api/v1",
            OPENAI_MODEL=model,
        )
    # New session so a timeout kills the whole harness tree (node CLIs spawn children).
    proc = subprocess.Popen(
        argv,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=scenario.timeout_s)
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        stderr += "\nharness timed out"
        code = None

    elapsed = time.monotonic() - started
    records = [r for r in audit_since(project, offset) if r.get("event") == "tool_call"]
    answer = final_text_opencode(stdout) if harness == "opencode" else final_text_qwen(stdout)
    runs = [r for r in records if r["tool"] == "run"]
    outputs = [r for r in records if r["tool"] == "output"]
    checks = {
        "answer": scenario.expect in answer,
        "run_called": any(r["outcome"].get("exit_code") == 0 for r in runs),
    }
    if scenario.needs_output_tool:
        checks["output_tool"] = any("error" not in r["outcome"] for r in outputs)
    if scenario.min_duration_ms:
        checks["long_run"] = any(
            r["outcome"].get("duration_ms", 0) >= scenario.min_duration_ms for r in runs
        )
    log = BASE / "logs" / f"{harness}-{model.replace('/', '_')}-{scenario.name}"
    log.parent.mkdir(parents=True, exist_ok=True)
    # Not with_suffix(): model ids like glm-5.3 contain dots.
    log.parent.joinpath(log.name + ".stdout").write_text(stdout)
    log.parent.joinpath(log.name + ".stderr").write_text(stderr)
    return {
        "harness": harness,
        "model": model,
        "scenario": scenario.name,
        "ok": all(checks.values()),
        "checks": checks,
        "exit": code,
        "seconds": round(elapsed, 1),
        "run_calls": len(runs),
        "output_calls": len(outputs),
        "coercions": sum(len(r.get("coercions") or []) for r in records),
        "tool_errors": sum(1 for r in records if "error" in r["outcome"]),
        "answer": answer.strip()[:120],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--harness", nargs="+", default=["opencode", "qwen"], choices=["opencode", "qwen"]
    )
    parser.add_argument("--model", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--scenario", nargs="+", default=[s.name for s in SCENARIOS])
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args()
    if not PYZ.exists():
        print(f"missing {PYZ}; run scripts/build.py first", file=sys.stderr)
        return 2
    projects = {"opencode": setup_opencode(args.model), "qwen": setup_qwen(args.model)}
    if args.setup_only:
        print(f"configured {projects['opencode']} and {projects['qwen']}")
        print(f"approve once for Qwen: (cd {projects['qwen']} && qwen mcp approve envelope)")
        return 0
    results: list[dict[str, Any]] = []
    for harness in args.harness:
        for model in args.model:
            for scenario in (s for s in SCENARIOS if s.name in args.scenario):
                result = run_one(harness, projects[harness], model, scenario)
                results.append(result)
                print(json.dumps(result), flush=True)
    (BASE / "results.json").write_text(json.dumps(results, indent=2))
    failed = [r for r in results if not r["ok"]]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} passed; details in {BASE}/results.json and {BASE}/logs/"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
