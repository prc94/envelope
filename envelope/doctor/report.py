"""Doctor report: environment, shells, restrictions, smoke tests and paths."""

from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from envelope import __version__
from envelope.config import LoadResult
from envelope.protocol.server import CallContext
from envelope.services import Services
from envelope.shells.registry import Shell

MARK = "envelope-ok"
UTF8_SAMPLE = "Grüße ✓ Привет"
LARGE_LINES = 600


def _writable(path: Path, *, is_dir: bool) -> dict[str, Any]:
    directory = path if is_dir else path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".envelope-doctor-", delete=True):
            pass
        return {"path": str(path), "writable": True}
    except OSError as exc:
        return {"path": str(path), "writable": False, "error": exc.strerror or str(exc)}


def _windows_codepages() -> dict[str, int] | None:
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.windll.kernel32
    return {
        "console_input": int(kernel32.GetConsoleCP()),
        "console_output": int(kernel32.GetConsoleOutputCP()),
        "oem": int(kernel32.GetOEMCP()),
        "ansi": int(kernel32.GetACP()),
    }


def smoke_commands(shell: Shell) -> dict[str, str | None]:
    """Commands per smoke test; None means not applicable for this shell."""
    if shell.family == "powershell":
        large = "$n = 0\n" + "$n++\n" * LARGE_LINES + 'Write-Output "count=$n"\n'
        return {
            "trivial": f"Write-Output '{MARK}'",
            "large_script": large,
            "utf8": f"Write-Output '{UTF8_SAMPLE}'",
        }
    if shell.id == "cmd":
        return {"trivial": f"echo {MARK}", "large_script": None, "utf8": f"echo {UTF8_SAMPLE}"}
    if shell.id == "fish":
        large = "set n 0\n" + "set n (math $n + 1)\n" * LARGE_LINES + 'echo "count=$n"\n'
    else:
        large = "n=0\n" + "n=$((n+1))\n" * LARGE_LINES + 'echo "count=$n"\n'
    return {"trivial": f"echo {MARK}", "large_script": large, "utf8": f"echo '{UTF8_SAMPLE}'"}


EXPECTED = {"trivial": MARK, "large_script": f"count={LARGE_LINES}", "utf8": UTF8_SAMPLE}


async def _smoke(services: Services, shell: Shell) -> dict[str, Any]:
    from envelope.tools.run import RunTool

    tool = RunTool(services)
    results: dict[str, Any] = {}
    for name, command in smoke_commands(shell).items():
        if command is None:
            results[name] = {"status": "n/a"}
            continue
        ctx = CallContext("doctor", "2025-11-25", {"name": "envelope-doctor"}, None)
        result = await tool.call({"command": command, "shell": shell.id, "timeout_s": 30}, ctx)
        structured = result.structured or {}
        output = ""
        handle = structured.get("handle")
        if isinstance(handle, str):
            reader = services.spool.open(handle)
            output = "\n".join(text for _, _, text in reader.lines())
        ok = not result.is_error and structured.get("exit_code") == 0 and EXPECTED[name] in output
        entry: dict[str, Any] = {"status": "pass" if ok else "fail"}
        if not ok:
            entry["error"] = (result.text if result.is_error else output or result.text)[-500:]
        results[name] = entry
    return results


def build_report(config: LoadResult, services: Services, *, smoke: bool = True) -> dict[str, Any]:
    settings = services.settings
    registry = services.registry
    report: dict[str, Any] = {
        "envelope": __version__,
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "config": {
            "file": str(config.config_file) if config.config_file else None,
            "searched": [str(p) for p in config.searched],
            "warnings": list(config.warnings) + list(registry.warnings),
        },
        "shells": [shell.to_json() for shell in registry.shells.values()],
        "default_shell": registry.default,
        "paths": {
            "spool": _writable(services.spool.dir, is_dir=True),
            "audit": {
                **_writable(services.audit.path, is_dir=False),
                "enabled": services.audit.enabled,
            }
            if services.audit.path is not None
            else {"enabled": False},
            "default_cwd": services.default_cwd,
        },
        "restrictions": {},
        "smoke_tests": {},
    }
    if services.windows:
        from envelope.execution import windows

        report["restrictions"] = {
            "powershell": {
                shell.id: {
                    "language_mode": shell.language_mode,
                    "execution_policy": shell.execution_policy,
                }
                for shell in registry.available()
                if shell.family == "powershell"
            },
            "code_pages": _windows_codepages(),
            "envelope_job": {
                "established": windows.self_job_established(),
                "error": windows.self_job_error,
            },  # pyright: ignore[reportPrivateUsage]
        }
    if smoke:

        async def run_all() -> dict[str, Any]:
            return {shell.id: await _smoke(services, shell) for shell in registry.available()}

        report["smoke_tests"] = asyncio.run(run_all())
    failed = [
        f"{shell}: {test}"
        for shell, tests in report["smoke_tests"].items()
        for test, result in tests.items()
        if result["status"] == "fail"
    ]
    problems: list[str] = [f"smoke test failed: {item}" for item in failed]
    if registry.default is None:
        problems.append("no shell is available")
    if not report["paths"]["spool"]["writable"]:
        problems.append("spool directory is not writable")
    report["problems"] = problems
    report["ok"] = not problems
    del settings
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"envelope {report['envelope']}",
        f"python   {report['python']['version']} ({report['python']['executable']})",
        f"os       {report['os']['system']} {report['os']['release']} ({report['os']['machine']})",
        f"config   {report['config']['file'] or 'none (built-in defaults)'}",
    ]
    if not report["config"]["file"]:
        lines.append(f"         searched: {', '.join(report['config']['searched'])}")
    for warning in report["config"]["warnings"]:
        lines.append(f"         warning: {warning}")
    lines.append("")
    lines.append("shells")
    for shell in report["shells"]:
        marker = "*" if shell["id"] == report["default_shell"] else " "
        if shell["available"]:
            detail = f"{shell['version'] or '(version unknown)'}  {shell['path']}"
            extras = [
                f"{key.replace('_', ' ')}: {shell[key]}"
                for key in ("language_mode", "execution_policy")
                if shell.get(key)
            ]
            if extras:
                detail += "  [" + ", ".join(extras) + "]"
        else:
            detail = f"unavailable ({shell.get('reason')})"
        lines.append(f" {marker} {shell['id']:<11} {detail}")
    lines.append(f"   default: {report['default_shell'] or 'none'}")
    restrictions = cast(dict[str, Any], report.get("restrictions") or {})
    if restrictions:
        lines.append("")
        lines.append("restrictions")
        if restrictions.get("code_pages"):
            pages = restrictions["code_pages"]
            lines.append("   code pages: " + ", ".join(f"{k} {v}" for k, v in pages.items()))
        job = restrictions.get("envelope_job")
        if job:
            state = "established" if job["established"] else f"NOT established ({job['error']})"
            lines.append(f"   kill-on-close job: {state}")
    if report["smoke_tests"]:
        lines.append("")
        lines.append("smoke tests")
        for shell, tests in report["smoke_tests"].items():
            summary = ", ".join(f"{name} {result['status']}" for name, result in tests.items())
            lines.append(f"   {shell:<11} {summary}")
            for name, result in tests.items():
                if result["status"] == "fail":
                    error = str(result.get("error", "")).strip().replace("\n", "\n" + " " * 18)
                    lines.append(f"      {name} error: {error}")
    lines.append("")
    lines.append("paths")
    for key in ("spool", "audit"):
        info = report["paths"][key]
        if "path" in info:
            state = "writable" if info["writable"] else f"NOT writable ({info.get('error')})"
            if key == "audit" and not info.get("enabled", True):
                state += ", disabled"
            lines.append(f"   {key:<6} {info['path']} ({state})")
    lines.append(f"   cwd    {report['paths']['default_cwd']}")
    lines.append("")
    lines.append(
        "status: OK" if report["ok"] else "status: PROBLEMS\n  " + "\n  ".join(report["problems"])
    )
    return "\n".join(lines)


def run_doctor(config: LoadResult, *, as_json: bool) -> int:
    from envelope.app import create_services

    services = create_services(config)
    report = build_report(config, services)
    services.backend.shutdown()
    out = sys.stdout
    if not out.isatty() and hasattr(out, "reconfigure"):
        # Piped on Windows, stdout would use the ANSI code page and fail on other scripts.
        cast(io.TextIOWrapper, out).reconfigure(encoding="utf-8")
    if as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_report(report) + "\n")
    sys.stdout.flush()
    os.environ.pop("ENVELOPE_DOCTOR", None)
    return 0 if report["ok"] else 1
