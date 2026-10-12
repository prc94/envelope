"""Long-lived services shared by the tools (built once at startup)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from envelope import paths
from envelope.audit import AuditLog
from envelope.config.settings import Settings
from envelope.execution.backend import Backend
from envelope.execution.runner import ManagedProcess
from envelope.execution.table import ProcessTable, end_reason
from envelope.output.decode import console_codepage
from envelope.output.spool import SpoolManager
from envelope.policy.decide import make_decide
from envelope.policy.gateway import Gateway
from envelope.protocol.server import ToolResult
from envelope.shells.registry import Platform, Registry


@dataclass
class Services:
    settings: Settings
    registry: Registry
    spool: SpoolManager
    audit: AuditLog
    processes: ProcessTable
    gateway: Gateway
    backend: Backend
    platform: Platform
    default_cwd: str
    environ: Mapping[str, str]
    config_file: Path | None = None
    codepage: str = "cp437"

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    def finalize_process(self, managed: ManagedProcess) -> None:
        """Tear down a finished process's spool and table entry once some
        call has observed and reported its end -- the original `run` call if
        it never yielded, or whichever later `run`/`output`/`stop`/
        `processes` call first sees it finished otherwise. Idempotent via
        `final_seen` (design B3)."""
        if managed.final_seen or not managed.finished:
            return
        managed.final_seen = True
        if managed.outcome is not None:
            # Persisted in the spool's own meta.json (bounded by its normal
            # retention, unlike an in-memory table entry) so `output`/`stop`
            # can still report the final status after this call forgets the
            # `ManagedProcess` below.
            managed.writer.meta.info["exit_code"] = managed.outcome.exit_code
            managed.writer.meta.info["signal"] = managed.outcome.signal
            if managed.name is not None:
                managed.writer.meta.info["name"] = managed.name
        managed.writer.finish()
        self.spool.release(managed.writer)
        self.processes.forget(managed.handle)

    def claim_notice_text(self) -> str | None:
        """Design B10: claim one ended, unseen process (if any) and render it
        as a `note:` line, finalizing it in the same step -- this *is* the
        model seeing its final status."""
        managed = self.processes.claim_notice()
        if managed is None:
            return None
        outcome = managed.outcome
        assert outcome is not None
        status = (
            f"exit {outcome.exit_code}" if outcome.signal is None else f"signal {outcome.signal}"
        )
        name_part = f' "{managed.name}"' if managed.name is not None else ""
        command = managed.request.command.strip().replace("\n", " ")
        if len(command) > 80:
            command = command[:79] + "…"
        text = (
            f"note: {managed.handle}{name_part} {status} ({end_reason(managed)}) "
            f"after {outcome.duration_s:.1f}s -- {command}"
        )
        self.audit.write({"event": "completion_notice", "handle": managed.handle})
        self.finalize_process(managed)
        return text

    def attach_notice(self, result: ToolResult) -> ToolResult:
        """Piggyback one completion notice onto any tool result (design B10);
        called right before a result is returned, by every tool."""
        text = self.claim_notice_text()
        if text is None:
            return result
        structured = dict(result.structured) if result.structured is not None else {}
        structured["notices"] = [*structured.get("notices", []), text]
        return ToolResult(f"{result.text}\n{text}", structured, is_error=result.is_error)


def spool_manager(settings: Settings) -> SpoolManager:
    directory = (
        Path(settings.spool_dir).expanduser() if settings.spool_dir else paths.default_spool_dir()
    )
    return SpoolManager(
        directory,
        cap_bytes=settings.spool_cap_bytes,
        total_bytes=settings.spool_total_bytes,
        retention_s=settings.spool_retention_h * 3600,
    )


def audit_log(settings: Settings) -> AuditLog:
    path = (
        Path(settings.audit_log).expanduser() if settings.audit_log else paths.default_audit_log()
    )
    return AuditLog(path, enabled=settings.audit_enabled)


def build_services(
    settings: Settings,
    registry: Registry,
    backend: Backend,
    *,
    environ: Mapping[str, str] | None = None,
    config_file: Path | None = None,
) -> Services:
    environ = dict(os.environ if environ is None else environ)
    audit = audit_log(settings)
    windows = registry.platform == "win32"
    default_cwd = os.path.abspath(os.path.expanduser(settings.default_cwd or os.getcwd()))
    return Services(
        settings=settings,
        registry=registry,
        spool=spool_manager(settings),
        audit=audit,
        processes=ProcessTable(
            audit, max_running=settings.max_running, evict_policy=settings.evict_policy
        ),
        gateway=Gateway(settings, audit, make_decide(windows=windows)),
        backend=backend,
        platform=registry.platform,
        default_cwd=default_cwd,
        environ=environ,
        config_file=config_file,
        codepage=console_codepage(),
    )
