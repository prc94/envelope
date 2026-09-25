"""Long-lived services shared by the tools (built once at startup)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from envelope import paths
from envelope.audit import AuditLog
from envelope.config.settings import Settings
from envelope.execution.backend import Backend
from envelope.output.decode import console_codepage
from envelope.output.spool import SpoolManager
from envelope.policy.decide import make_decide
from envelope.policy.gateway import Gateway
from envelope.shells.registry import Platform, Registry


@dataclass
class Services:
    settings: Settings
    registry: Registry
    spool: SpoolManager
    audit: AuditLog
    gateway: Gateway
    backend: Backend
    platform: Platform
    default_cwd: str
    environ: Mapping[str, str]
    config_file: Path | None = None
    codepage: str = "cp437"
    _limiter: asyncio.Semaphore | None = field(default=None, repr=False)

    @property
    def limiter(self) -> asyncio.Semaphore:
        if self._limiter is None:
            self._limiter = asyncio.Semaphore(self.settings.max_concurrency)
        return self._limiter

    @property
    def windows(self) -> bool:
        return self.platform == "win32"


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
        gateway=Gateway(settings, audit, make_decide(windows=windows)),
        backend=backend,
        platform=registry.platform,
        default_cwd=default_cwd,
        environ=environ,
        config_file=config_file,
        codepage=console_codepage(),
    )
