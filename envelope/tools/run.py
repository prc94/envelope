"""The ``run`` tool: execute a command in a chosen shell and return a bounded result."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from envelope.audit import call_record
from envelope.execution.backend import SpawnError
from envelope.execution.environment import build_env
from envelope.execution.runner import RunOutcome, RunRequest, execute
from envelope.output.render import human_bytes, render_run_output
from envelope.output.spool import SpoolWriter
from envelope.policy.gateway import PolicyRefusal, audit_only_note, refusal_text
from envelope.policy.model import ExecRequest
from envelope.protocol.server import CallContext, ToolResult
from envelope.render.hints import HintContext, hints_for
from envelope.render.result import RunReport, run_structured, run_text
from envelope.services import Services
from envelope.shells.gitbash import msys_path_dirs
from envelope.shells.launch import LaunchError, build_launch
from envelope.shells.pathconv import to_windows
from envelope.shells.registry import Shell, ShellError
from envelope.shells.resolve import find_executable
from envelope.tools.coercion import ArgumentError, Param, coerce

PARAMS = (
    Param("command", "string", "must be the command text (a string)"),
    Param("shell", "string", "must be a shell name (a string)"),
    Param("cwd", "string", "must be a directory path (a string)"),
    Param("timeout_s", "number", "must be a number of seconds"),
    Param("stdin", "string", "must be the text to send to standard input (a string)"),
    Param("env", "env", "must be an object of environment variables or KEY=value lines"),
)
HINT_TAIL_LINES = 60


class ToolFailure(Exception):
    """An operation envelope could not perform (rendered with isError)."""


def _shell_list(services: Services) -> str:
    items: list[str] = []
    for shell in services.registry.available():
        text = shell.describe()
        if shell.id == services.registry.default:
            text += " (default)"
        items.append(text)
    return ", ".join(items) or "none detected"


def run_definition(services: Services) -> dict[str, Any]:
    s = services.settings
    default = services.registry.default or "none"
    description = (
        "Run a shell command or script in a non-interactive terminal session and get its exit code "
        f"and output. Available shells: {_shell_list(services)}. Use it for builds, tests, git, "
        "package managers, file and system operations, bash scripts, PowerShell cmdlets and cmd. "
        "Each call starts a fresh shell process: cd, exports and variables do NOT carry over "
        "between calls, so pass cwd and env instead. Stdin is closed unless you pass stdin, so "
        "interactive prompts fail fast. Multi-line scripts of any length are fine. Long output is "
        "shortened to its first and last lines; the result then names a handle (out_...) whose "
        "full output the `output` tool can page or grep."
    )
    return {
        "name": "run",
        "title": "Run shell command",
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command or multi-line script, in the syntax of the chosen shell.",
                },
                "shell": {
                    "type": "string",
                    "description": (
                        f"Shell to run in (default: {default}). One of: "
                        f"{', '.join(services.registry.available_ids()) or 'none'}."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": f"Working directory (default: {services.default_cwd})."
                    + (
                        " Windows, C:/ or /c/ style paths are accepted." if services.windows else ""
                    ),
                },
                "timeout_s": {
                    "type": "number",
                    "description": (
                        f"Timeout in seconds (default {s.default_timeout_s:g}, max {s.max_timeout_s:g}); "
                        "the whole process tree is killed when it expires."
                    ),
                },
                "stdin": {
                    "type": "string",
                    "description": "Text written to the command's standard input, followed by end-of-file.",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables for this call only.",
                },
            },
            "required": ["command"],
        },
        "annotations": {
            "title": "Run shell command",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    }


def resolve_cwd(services: Services, requested: str | None) -> str:
    if requested is None or not requested.strip():
        path = services.default_cwd
    else:
        raw = requested.strip()
        if services.windows:
            raw = to_windows(raw)
        raw = os.path.expanduser(raw)
        path = raw if os.path.isabs(raw) else os.path.join(services.default_cwd, raw)
    path = os.path.normpath(path)
    if not os.path.exists(path):
        raise ToolFailure(f"working directory does not exist: {path}")
    if not os.path.isdir(path):
        raise ToolFailure(f"working directory is not a directory: {path}")
    return path


def make_locator(services: Services, current: Shell):
    """Cross-shell PATH lookup for the command-not-found hint (no processes spawned)."""

    def locate(name: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        host_path = services.environ.get("PATH") or services.environ.get("Path") or ""
        pathext = services.environ.get("PATHEXT")
        for shell in services.registry.available():
            if shell.id == current.id or shell.kind == "custom":
                continue
            if services.windows:
                search = host_path
                if shell.id == "git-bash" and shell.git_root:
                    search = ";".join([*msys_path_dirs(shell.git_root), host_path])
                path = find_executable(name, search, windows=True, pathext=pathext)
            else:
                path = find_executable(name, host_path, windows=False)
            if path is not None:
                found.append((shell.id, path))
        return found

    return locate


class RunTool:
    name = "run"

    def __init__(self, services: Services) -> None:
        self.services = services
        self._definition = run_definition(services)

    def definition(self) -> dict[str, Any]:
        return self._definition

    async def call(self, arguments: dict[str, Any], ctx: CallContext) -> ToolResult:
        started = time.monotonic()
        coercions: list[dict[str, Any]] = list(ctx.extra.get("coercions", []))
        record: dict[str, Any] = {
            "client": ctx.client_info,
            "tool": self.name,
            "arguments": arguments,
            "coercions": coercions,
            "request": None,
            "decision": None,
        }
        try:
            result = await self._run(arguments, ctx, started, record)
        except ToolFailure as exc:
            self._audit(record, {"error": str(exc)})
            return ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
        except asyncio.CancelledError:
            self._audit(
                record,
                {"cancelled": True, "duration_ms": round((time.monotonic() - started) * 1000)},
            )
            raise
        return result

    def _audit(self, record: dict[str, Any], outcome: dict[str, Any]) -> None:
        self.services.audit.write(call_record(**record, outcome=outcome))

    async def _run(
        self, arguments: dict[str, Any], ctx: CallContext, started: float, record: dict[str, Any]
    ) -> ToolResult:
        services = self.services
        settings = services.settings
        try:
            values, coercions = coerce(arguments, PARAMS)
        except ArgumentError as exc:
            raise ToolFailure(str(exc)) from None
        record["coercions"].extend(coercions)
        record["arguments"] = values
        command = values.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolFailure("`command` is required and must be a non-empty string")
        try:
            shell = services.registry.resolve(values.get("shell"))
        except ShellError as exc:
            raise ToolFailure(str(exc)) from None
        cwd = resolve_cwd(services, values.get("cwd"))

        notes: list[str] = []
        timeout = float(values.get("timeout_s", settings.default_timeout_s))
        if timeout <= 0:
            raise ToolFailure("`timeout_s` must be a positive number of seconds")
        if timeout > settings.max_timeout_s:
            notes.append(
                f"note: timeout_s {timeout:g} exceeds the maximum; clamped to {settings.max_timeout_s:g}s"
            )
            timeout = settings.max_timeout_s
        deadline = started + timeout
        explicit_env: dict[str, str] = values.get("env", {})
        stdin: str | None = values.get("stdin")

        assert shell.path is not None
        request = ExecRequest(
            shell=shell.id,
            shell_path=shell.path,
            command=command,
            cwd=cwd,
            env=explicit_env,
            stdin_supplied=stdin is not None,
            timeout_s=timeout,
            client=ctx.client_info,
        )
        record["request"] = request.to_json()
        try:
            permit = services.gateway.authorize(request)
        except PolicyRefusal as refusal:
            record["decision"] = refusal.decision.to_json()
            text = refusal_text(refusal.decision)
            self._audit(record, {"refused": refusal.decision.verdict})
            return ToolResult(text, {"decision": refusal.decision.to_json()}, is_error=True)
        record["decision"] = permit.decision.to_json()
        if (note := audit_only_note(permit.decision)) is not None:
            notes.append(note)

        built = build_env(
            services.environ,
            scrub=settings.env_scrub,
            defaults=settings.env_defaults,
            explicit=explicit_env,
            windows=services.windows,
        )
        writer = services.spool.create(
            {"shell": shell.id, "cwd": cwd, "command": command[:200]},
            fallback_codepage=None,
        )
        try:
            try:
                launch = build_launch(shell, command, writer.dir)
            except LaunchError as exc:
                raise ToolFailure(str(exc)) from None
            if launch.console_codepage:
                writer.set_fallback_codepage(services.codepage)

            ctx.status = lambda: _status(writer, started)
            try:
                await asyncio.wait_for(
                    services.limiter.acquire(), max(deadline - time.monotonic(), 0)
                )
            except TimeoutError:
                outcome = RunOutcome(None, None, True, False, time.monotonic() - started, None)
                notes.append(
                    f"note: all {settings.max_concurrency} execution slots were busy; the command never started"
                )
                return self._finish(
                    shell,
                    cwd,
                    outcome,
                    writer,
                    notes,
                    record,
                    request,
                    stdin,
                    timeout,
                    permit.decision.to_json(),
                )
            try:
                outcome = await execute(
                    services.backend,
                    RunRequest(
                        shell.id, launch, cwd, {**built.env, **launch.env}, stdin, deadline, timeout
                    ),
                    writer,
                    permit=permit,
                )
            except SpawnError as exc:
                raise ToolFailure(exc.describe()) from None
            finally:
                services.limiter.release()
        except BaseException:
            writer.finish()
            services.spool.release(writer)
            raise
        if built.removed:
            record["env_removed"] = built.removed
        return self._finish(
            shell,
            cwd,
            outcome,
            writer,
            notes,
            record,
            request,
            stdin,
            timeout,
            permit.decision.to_json(),
        )

    def _finish(
        self,
        shell: Shell,
        cwd: str,
        outcome: RunOutcome,
        writer: SpoolWriter,
        notes: list[str],
        record: dict[str, Any],
        request: ExecRequest,
        stdin: str | None,
        timeout: float,
        decision: dict[str, Any],
    ) -> ToolResult:
        services = self.services
        settings = services.settings
        meta = writer.finish()
        services.spool.release(writer)
        reader = services.spool.open(meta.handle)
        rendered = render_run_output(reader, settings.output_max_chars, settings.output_max_lines)
        tail = (
            [text for _, _, text in reader.lines_reversed()][:HINT_TAIL_LINES][::-1]
            if reader.total_lines
            else []
        )
        from envelope.output.cleanup import clean_line

        tail = [clean_line(t) for t in tail]
        hints = hints_for(
            HintContext(
                shell=shell.id,
                exit_code=outcome.exit_code,
                signal=outcome.signal,
                timed_out=outcome.timed_out,
                timeout_s=timeout,
                max_timeout_s=settings.max_timeout_s,
                tree_terminated=outcome.tree_terminated,
                output_tail=tail,
                last_partial=tail[-1] if tail and meta.ends_without_newline else None,
                stdin_supplied=stdin is not None,
                binary=[
                    (n, s.bytes, s.binary_type)
                    for n, s in meta.streams.items()
                    if s.binary and s.bytes
                ],
                replaced=sum(s.replaced for s in meta.streams.values()),
                encodings={n: s.encoding or "" for n, s in meta.streams.items() if s.replaced},
                ps_errors=outcome.ps_errors,
                locate=make_locator(services, shell),
            )
        )
        report = RunReport(
            shell=shell.id,
            cwd=cwd,
            duration_s=outcome.duration_s,
            exit_code=outcome.exit_code,
            signal=outcome.signal,
            timed_out=outcome.timed_out,
            timeout_s=timeout,
            meta=meta,
            rendered=rendered,
            hints=hints,
            notes=notes,
            decision={"verdict": decision["verdict"], "rule": decision["rule"]},
        )
        self._audit(
            record,
            {
                "exit_code": outcome.exit_code,
                "signal": outcome.signal,
                "timed_out": outcome.timed_out,
                "duration_ms": round(outcome.duration_s * 1000),
                "handle": meta.handle,
                "stdout_bytes": meta.streams["stdout"].bytes,
                "stderr_bytes": meta.streams["stderr"].bytes,
                "lines": meta.total_lines,
                "hints": [h.id for h in hints],
            },
        )
        return ToolResult(run_text(report), run_structured(report))


def _status(writer: SpoolWriter, started: float) -> str:
    elapsed = time.monotonic() - started
    size = human_bytes(writer.meta.total_raw_bytes)
    last = writer.last_line.strip()
    if len(last) > 80:
        last = last[:77] + "..."
    return f"running {elapsed:.0f}s, {size} output" + (f", last: {last}" if last else "")
