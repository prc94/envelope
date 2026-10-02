"""The ``run`` tool: execute a command in a chosen shell and return a bounded result."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from envelope.audit import call_record
from envelope.execution.backend import SpawnError
from envelope.execution.environment import BuiltEnv, build_env
from envelope.execution.ports import listening_ports
from envelope.execution.runner import ManagedProcess, RunOutcome, RunRequest, start
from envelope.execution.table import SlotsRefused, end_reason, validate_name
from envelope.execution.wait import WaitCondition
from envelope.output.render import human_bytes, render_run_output
from envelope.output.spool import STDOUT, LiveSpoolReader, SpoolView, SpoolWriter
from envelope.policy.gateway import Permit, PolicyRefusal, audit_only_note, refusal_text
from envelope.policy.model import ExecRequest
from envelope.protocol.server import CallContext, ToolResult
from envelope.render.hints import HintContext, extract_port_in_use, hints_for
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
    Param("wait_s", "number", "must be 0 or a positive number of seconds"),
    Param(
        "wait_for",
        "wait_for",
        "must be 'exit', 'regex:<pattern>', 'port:<n>', 'idle:<seconds>' or an object form",
    ),
    Param("stdin", "string", "must be the text to send to standard input (a string)"),
    Param("env", "env", "must be an object of environment variables or KEY=value lines"),
    Param("name", "string", "must be 1-40 characters of letters, digits, '.', '_' or '-'"),
    Param("restart", "boolean", "must be a boolean"),
)
HINT_TAIL_LINES = 60
_EXIT_CONDITION = WaitCondition("exit", "exit")
_WAIT_OUTCOME_NOTES = {
    "condition_met": "note: wait condition met",
    "finished": "note: the process finished before the wait condition was met",
    "wait_expired": "note: the wait expired before the condition was met",
}


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
        "full output the `output` tool can page or grep. A command still running after wait_s is "
        "handed back as a background process and keeps running; name a long-lived process (a "
        "server, a watcher) to make a later call join or restart it instead of starting a copy."
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
                        "Kills the command and everything it started, also after it was handed "
                        "back ("
                        + (
                            f"default {s.default_timeout_s:g}s"
                            if s.default_timeout_s is not None
                            else "no default"
                        )
                        + f", max {s.max_timeout_s:g}s). Omit it for servers and watchers you "
                        "intend to keep running."
                    ),
                },
                "wait_s": {
                    "type": "number",
                    "description": (
                        f"How long to wait for the command to finish before returning a running "
                        f"result with a handle instead (default {s.wait_default_s:g}, max "
                        f"{s.wait_max_s:g}); the command keeps running either way. Use 0 to check "
                        "a fast command without blocking."
                    ),
                },
                "wait_for": {
                    "type": ["string", "object"],
                    "description": (
                        "Condition to wait for instead of the command finishing: 'exit' "
                        "(default), 'regex:<pattern>' (a line matches), 'port:<n>' (the "
                        "command listens on TCP port n) or 'idle:<seconds>' (no output for "
                        "that long). Bounded by wait_s either way."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Name a long-lived process (a server, watcher) so a later run() with the "
                        "same name joins it instead of starting a copy, and output()/stop() can "
                        "address it by name."
                    ),
                },
                "restart": {
                    "type": "boolean",
                    "description": (
                        "With name: stop the process currently running under that name (if any) "
                        "and start this command in its place."
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
            return self.services.attach_notice(
                ToolResult(f"error: {exc}", {"error": str(exc)}, is_error=True)
            )
        except asyncio.CancelledError:
            self._audit(
                record,
                {"cancelled": True, "duration_ms": round((time.monotonic() - started) * 1000)},
            )
            raise
        return self.services.attach_notice(result)

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
        name = values.get("name")
        restart = bool(values.get("restart", False))
        if name is not None:
            name_error = validate_name(name)
            if name_error is not None:
                raise ToolFailure(name_error)

        notes: list[str] = []
        timeout_value = values.get("timeout_s", settings.default_timeout_s)
        timeout: float | None = None if timeout_value is None else float(timeout_value)
        if timeout is not None and timeout <= 0:
            raise ToolFailure("`timeout_s` must be a positive number of seconds")
        if timeout is not None and timeout > settings.max_timeout_s:
            notes.append(
                f"note: timeout_s {timeout:g} exceeds the maximum; clamped to {settings.max_timeout_s:g}s"
            )
            timeout = settings.max_timeout_s
        deadline = started + timeout if timeout is not None else float("inf")
        lifetime_deadline = (
            started + settings.max_lifetime_s
            if settings.max_lifetime_s is not None
            else float("inf")
        )

        wait_s = float(values.get("wait_s", settings.wait_default_s))
        if wait_s < 0:
            raise ToolFailure("`wait_s` must be 0 or a positive number of seconds")
        if wait_s > settings.wait_max_s:
            notes.append(
                f"note: wait_s {wait_s:g} exceeds the maximum; clamped to {settings.wait_max_s:g}s"
            )
            wait_s = settings.wait_max_s
        wait_deadline = min(started + wait_s, deadline, lifetime_deadline)
        wait_for_value = values.get("wait_for")
        explicit_wait_for = wait_for_value is not None
        condition: WaitCondition = wait_for_value if wait_for_value is not None else _EXIT_CONDITION
        explicit_env: dict[str, str] = values.get("env", {})
        stdin: str | None = values.get("stdin")

        reserved_name: str | None = None
        restart_name: str | None = name if restart else None
        if name is not None and not restart:
            # Design B6: idempotent start -- join a live process under this
            # name instead of starting a copy. The check-and-reserve is
            # synchronous (no `await` in between), so two parallel calls for
            # a not-yet-running name can never both start a process: the
            # second one finds the first's placeholder and joins its result.
            while True:
                live = services.processes.find_live(name)
                if live is not None:
                    return await self._join(
                        live, command, condition, wait_deadline, explicit_wait_for, notes, record
                    )
                waiter = services.processes.begin_start(name)
                if waiter is None:
                    reserved_name = name
                    break
                joined = await waiter
                if joined is not None:
                    return await self._join(
                        joined, command, condition, wait_deadline, explicit_wait_for, notes, record
                    )
                # the previous starter failed before registering a process;
                # the name is free again -- loop and try reserving it

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
            name=name,
            restart=restart,
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

        restart_lock = (
            services.processes.name_lock(restart_name) if restart_name is not None else None
        )
        if restart_lock is not None:
            await restart_lock.acquire()
        try:
            if restart_name is not None:
                # Design B6/B7: the new command is validated and authorized
                # (above) *before* the old process is touched, and a
                # cancellation of this call still lets the stop finish even
                # though it no longer waits for the rest of the restart.
                old = services.processes.find_live(restart_name)
                if old is not None:
                    graceful = await asyncio.shield(old.stop(settings.stop_grace_s))
                    services.finalize_process(old)
                    notes.append(
                        f"note: stopped {old.handle} "
                        f"({'graceful' if graceful else 'forced'}) to restart '{restart_name}'"
                    )
            built = build_env(
                services.environ,
                scrub=settings.env_scrub,
                defaults=settings.env_defaults,
                explicit=explicit_env,
                windows=services.windows,
            )
            return await self._spawn_and_finish(
                shell,
                cwd,
                command,
                writer=services.spool.create(
                    {"shell": shell.id, "cwd": cwd, "command": command[:200]},
                    fallback_codepage=None,
                ),
                built=built,
                stdin=stdin,
                started=started,
                deadline=deadline,
                lifetime_deadline=lifetime_deadline,
                timeout=timeout,
                wait_deadline=wait_deadline,
                condition=condition,
                explicit_wait_for=explicit_wait_for,
                notes=notes,
                record=record,
                request=request,
                permit=permit,
                ctx=ctx,
                reserved_name=reserved_name,
                restart_name=restart_name,
            )
        finally:
            if restart_lock is not None:
                restart_lock.release()

    async def _spawn_and_finish(
        self,
        shell: Shell,
        cwd: str,
        command: str,
        *,
        writer: SpoolWriter,
        built: BuiltEnv,
        stdin: str | None,
        started: float,
        deadline: float,
        lifetime_deadline: float,
        timeout: float | None,
        wait_deadline: float,
        condition: WaitCondition,
        explicit_wait_for: bool,
        notes: list[str],
        record: dict[str, Any],
        request: ExecRequest,
        permit: Permit,
        ctx: CallContext,
        reserved_name: str | None,
        restart_name: str | None,
    ) -> ToolResult:
        services = self.services
        settings = services.settings
        managed: ManagedProcess | None = None
        try:
            try:
                launch = build_launch(shell, command, writer.dir)
            except LaunchError as exc:
                raise ToolFailure(str(exc)) from None
            if launch.console_codepage:
                writer.set_fallback_codepage(services.codepage)

            ctx.status = lambda: _status(writer, started)
            try:
                evicted = await services.processes.reserve()
            except SlotsRefused:
                outcome = RunOutcome(None, None, True, False, time.monotonic() - started, None)
                notes.append(
                    f"note: the live-process cap ({settings.max_running}) is full and nothing "
                    "could be freed; the command never started"
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
            if evicted is not None:
                notes.append(
                    f"note: {evicted.handle} was evicted (least recently used) to free a slot "
                    f"under max_running={settings.max_running}"
                )
            try:
                managed = await start(
                    services.backend,
                    RunRequest(
                        shell.id,
                        launch,
                        cwd,
                        {**built.env, **launch.env},
                        stdin,
                        deadline,
                        timeout,
                        command,
                        lifetime_deadline=lifetime_deadline,
                    ),
                    writer,
                    permit=permit,
                )
            except SpawnError as exc:
                services.processes.release()
                raise ToolFailure(exc.describe()) from None
            duplicate = _same_command_note(services, managed, shell.id, cwd, command)
            if duplicate is not None:
                notes.append(duplicate)
            services.processes.commit(managed)
            if restart_name is not None:
                services.processes.assign_name(restart_name, managed)
            elif reserved_name is not None:
                services.processes.finish_start(reserved_name, managed)
            yielded = await _wait_or_yield(managed, wait_deadline, condition)
            if yielded.wait_result == "wait_expired" and wait_deadline >= min(
                deadline, lifetime_deadline
            ):
                # wait_s was clamped to (or already exceeded) a kill deadline
                # and the condition was never met: this is a genuine timeout,
                # not a yield -- wait (unbounded) for the forced kill to
                # actually finish. A condition met (or the process finishing
                # on its own) while still short of that deadline is not a
                # timeout, even though `outcome` is `None` for the first case.
                yielded = await _wait_or_yield(managed, float("inf"), _EXIT_CONDITION)
            wait_outcome = yielded.wait_result if explicit_wait_for else None
            if (
                wait_outcome == "condition_met"
                and condition.kind == "port"
                and managed.port_verified is False
            ):
                notes.append(
                    "note: port ownership could not be verified on this platform; a local "
                    "connection succeeded, but this may not be the process this call started"
                )
            if yielded.outcome is None:
                return self._running_result(
                    shell, cwd, managed, writer, notes, record, wait_outcome=wait_outcome
                )
            outcome = yielded.outcome
        except BaseException:
            writer.finish()
            services.spool.release(writer)
            raise
        finally:
            if reserved_name is not None and managed is None:
                services.processes.finish_start(reserved_name, None)
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
            wait_outcome=wait_outcome,
            managed=managed,
        )

    def _finish(
        self,
        shell: Shell,
        cwd: str,
        outcome: RunOutcome,
        writer: SpoolWriter,
        notes: list[str],
        record: dict[str, Any],
        request: ExecRequest | None,
        stdin: str | None,
        timeout: float | None,
        decision: dict[str, Any] | None,
        wait_outcome: str | None = None,
        managed: ManagedProcess | None = None,
    ) -> ToolResult:
        services = self.services
        settings = services.settings
        # `max_lifetime_s` is a second, independent deadline (design B9); for
        # display purposes a kill from either one is "timed out" against
        # whichever limit actually fired.
        ended_by_timer = outcome.timed_out or outcome.lifetime_exceeded
        if outcome.timed_out and timeout is not None:
            displayed_limit = timeout
        elif outcome.lifetime_exceeded and settings.max_lifetime_s is not None:
            displayed_limit = settings.max_lifetime_s
        else:
            displayed_limit = timeout or 0.0
        if managed is not None:
            # Reported normally: no later call needs to account for this
            # process's end itself (design B3's `final_seen`).
            services.finalize_process(managed)
        else:
            writer.finish()
            services.spool.release(writer)
        meta = writer.meta
        reader = services.spool.open(meta.handle)
        rendered = render_run_output(reader, settings.output_max_chars, settings.output_max_lines)
        tail = (
            [text for _, _, text in reader.lines_reversed()][:HINT_TAIL_LINES][::-1]
            if reader.total_lines
            else []
        )
        from envelope.output.cleanup import clean_line

        tail = [clean_line(t) for t in tail]
        port_owner = None
        if (port := extract_port_in_use(tail)) is not None:
            for other in services.processes.all():
                if managed is not None and other is managed:
                    continue
                pids = other.process.tree_pids()
                if pids and port in (listening_ports(pids) or frozenset()):
                    port_owner = f"{other.handle}" + (f' ("{other.name}")' if other.name else "")
                    break
        hints = hints_for(
            HintContext(
                shell=shell.id,
                exit_code=outcome.exit_code,
                signal=outcome.signal,
                timed_out=ended_by_timer,
                timeout_s=displayed_limit,
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
                command=request.command if request is not None else "",
                leftover_count=outcome.leftover_count,
                port_owner=port_owner,
                locate=make_locator(services, shell),
            )
        )
        if wait_outcome is not None:
            notes = [*notes, _WAIT_OUTCOME_NOTES[wait_outcome]]
        report = RunReport(
            shell=shell.id,
            cwd=cwd,
            duration_s=outcome.duration_s,
            exit_code=outcome.exit_code,
            signal=outcome.signal,
            timed_out=ended_by_timer,
            timeout_s=displayed_limit,
            meta=meta,
            rendered=rendered,
            hints=hints,
            notes=notes,
            decision=(
                {"verdict": decision["verdict"], "rule": decision["rule"]}
                if decision is not None
                else None
            ),
            end_reason=end_reason(managed) if managed is not None else None,
            name=managed.name if managed is not None else None,
            extra={"wait_outcome": wait_outcome} if wait_outcome is not None else {},
        )
        self._audit(
            record,
            {
                "state": "ended",
                "end_reason": end_reason(managed) if managed is not None else None,
                "name": managed.name if managed is not None else None,
                "exit_code": outcome.exit_code,
                "signal": outcome.signal,
                "timed_out": outcome.timed_out,
                "lifetime_exceeded": outcome.lifetime_exceeded,
                "duration_ms": round(outcome.duration_s * 1000),
                "handle": meta.handle,
                "stdout_bytes": meta.streams["stdout"].bytes,
                "stderr_bytes": meta.streams["stderr"].bytes,
                "lines": meta.total_lines,
                "hints": [h.id for h in hints],
                **({"wait": {"outcome": wait_outcome}} if wait_outcome is not None else {}),
            },
        )
        return ToolResult(run_text(report), run_structured(report))

    def _running_result(
        self,
        shell: Shell,
        cwd: str,
        managed: ManagedProcess,
        writer: SpoolWriter,
        notes: list[str],
        record: dict[str, Any],
        wait_outcome: str | None = None,
    ) -> ToolResult:
        """Design B1/B3: the command is still running. The spool stays live
        (no ``writer.finish()``/``services.spool.release()``) and the process
        stays registered in the table -- ``output`` can keep reading it and,
        eventually, its own normal completion or the shutdown sweep accounts
        for its end."""
        services = self.services
        settings = services.settings
        handle = writer.meta.handle
        managed.handed_back = True
        reader = services.spool.open(handle)
        rendered = render_run_output(reader, settings.output_max_chars, settings.output_max_lines)
        elapsed = time.monotonic() - managed.started
        notes = [
            *notes,
            f"note: still running -- do not start it again; continue with "
            f'output(handle="{handle}") or stop(handle="{handle}")',
        ]
        if wait_outcome is not None:
            notes.append(_WAIT_OUTCOME_NOTES[wait_outcome])
        report = RunReport(
            shell=shell.id,
            cwd=cwd,
            duration_s=elapsed,
            exit_code=None,
            running=True,
            unterminated_lines=_unterminated_lines(reader),
            meta=reader.meta,
            rendered=rendered,
            notes=notes,
            name=managed.name,
            extra={"wait_outcome": wait_outcome} if wait_outcome is not None else {},
        )
        self._audit(
            record,
            {
                "state": "running",
                "handle": handle,
                "name": managed.name,
                "duration_ms": round(elapsed * 1000),
                **({"wait": {"outcome": wait_outcome}} if wait_outcome is not None else {}),
            },
        )
        return ToolResult(run_text(report), run_structured(report))

    async def _join(
        self,
        live: ManagedProcess,
        command: str,
        condition: WaitCondition,
        wait_deadline: float,
        explicit_wait_for: bool,
        notes: list[str],
        record: dict[str, Any],
    ) -> ToolResult:
        """Design B6: a `run(name=...)` call found a live process under that
        name -- becomes a wait on it instead of starting a new one. No policy
        decision (nothing executes); cancelling this call ends only the wait
        (design B6/mcp-server Cancellation), unlike a `run` that actually
        started something."""
        services = self.services
        notes = list(notes)
        if command != live.request.command:
            notes.append(
                f"note: '{live.name}' is already running a different command "
                f"({live.request.command[:80]!r}); the requested command did not start"
            )
        record["decision"] = None
        record["executes"] = False
        result = await live.wait_condition(condition, wait_deadline)
        wait_outcome = result if explicit_wait_for else None
        live_shell = services.registry.resolve(live.request.shell_id)
        if live.outcome is None:
            return self._running_result(
                live_shell, live.request.cwd, live, live.writer, notes, record, wait_outcome
            )
        return self._finish(
            live_shell,
            live.request.cwd,
            live.outcome,
            live.writer,
            notes,
            record,
            None,
            None,
            None,
            None,
            wait_outcome=wait_outcome,
            managed=live,
        )


@dataclass
class _Yielded:
    outcome: RunOutcome | None
    """``None`` while still running (design B1) -- to be handed back with a
    handle; set once the process has finished, regardless of why the wait
    ended."""
    wait_result: str
    """``"condition_met"``, ``"finished"`` or ``"wait_expired"`` (design B5)."""


async def _wait_or_yield(
    managed: ManagedProcess, wait_deadline: float, condition: WaitCondition
) -> _Yielded:
    """Wait up to ``wait_deadline`` for ``condition``; the process keeps
    running either way -- this never itself terminates it on a plain
    timeout/expiry. A cancellation here, though, must still kill the tree
    ``run`` itself started (design B1/B3) -- unlike a later join or
    `output`/`processes` wait (task 4.6), which only ends the wait -- so,
    unlike ``ManagedProcess.wait()``/``wait_condition()`` on their own, this
    forces termination before letting the cancellation propagate."""
    try:
        result = await managed.wait_condition(condition, wait_deadline)
    except asyncio.CancelledError:
        await asyncio.shield(managed.terminate())
        await asyncio.shield(managed.wait(float("inf")))
        raise
    outcome = managed.outcome if result != "wait_expired" else None
    return _Yielded(outcome, result)


def _same_command_note(
    services: Services, managed: ManagedProcess, shell_id: str, cwd: str, command: str
) -> str | None:
    """Design B6: models tend to re-issue a command after a running result;
    note any other unnamed process already running the same command, shell
    and cwd, so the model can use its handle instead of starting another."""
    for other in services.processes.all():
        if (
            other is not managed
            and other.name is None
            and not other.finished
            and other.request.shell_id == shell_id
            and other.request.cwd == cwd
            and other.request.command == command
        ):
            return f"note: {other.handle} is still running this exact command"
    return None


def _unterminated_lines(reader: SpoolView) -> tuple[str, ...]:
    if not isinstance(reader, LiveSpoolReader):
        return ()
    lines: list[str] = []
    for stream, text in reader.unterminated_lines().items():
        if text:
            label = "stdout" if stream == STDOUT else "stderr"
            lines.append(f"[{label}, unterminated] {text}")
    return tuple(lines)


def _status(writer: SpoolWriter, started: float) -> str:
    elapsed = time.monotonic() - started
    size = human_bytes(writer.meta.total_raw_bytes)
    last = writer.last_line.strip()
    if len(last) > 80:
        last = last[:77] + "..."
    return f"running {elapsed:.0f}s, {size} output" + (f", last: {last}" if last else "")
