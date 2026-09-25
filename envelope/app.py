"""Wire services, tools and the protocol server; serve MCP over stdio."""

from __future__ import annotations

import asyncio
import sys
from typing import BinaryIO

from envelope import __version__
from envelope.config import LoadResult
from envelope.execution.backend import Backend, default_backend
from envelope.instructions import build_instructions
from envelope.log import logger
from envelope.protocol.server import Server
from envelope.protocol.transport import ThreadedLineSource, stream_writer
from envelope.services import Services, build_services
from envelope.shells.registry import detect_host
from envelope.tools.output import OutputTool
from envelope.tools.run import RunTool

SERVER_INFO = {"name": "envelope", "title": "Envelope shell runner", "version": __version__}


def create_services(config: LoadResult, backend: Backend | None = None) -> Services:
    settings = config.settings
    registry = detect_host(settings.shells, settings.default_shell)
    for warning in registry.warnings:
        logger.warning("%s", warning)
    services = build_services(
        settings, registry, backend or default_backend(), config_file=config.config_file
    )
    try:
        services.spool.ensure_dir()
        services.spool.cleanup()
    except OSError as exc:
        logger.error("spool directory %s is not usable: %s", services.spool.dir, exc)
    return services


def create_server(services: Services) -> Server:
    return Server(
        tools=[RunTool(services), OutputTool(services)],
        server_info=SERVER_INFO,
        instructions=build_instructions(services.registry),
        progress_interval_s=services.settings.progress_interval_s,
        on_shutdown=lambda: asyncio.to_thread(services.backend.shutdown),
    )


def serve(config: LoadResult, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    # Keep the protocol stream exclusively ours: anything printed by accident goes to stderr.
    protocol_out = stdout or sys.stdout.buffer
    protocol_in = stdin or sys.stdin.buffer
    sys.stdout = sys.stderr
    services = create_services(config)
    server = create_server(services)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        await server.serve(ThreadedLineSource(protocol_in, loop), stream_writer(protocol_out))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        services.backend.shutdown()
    logger.debug("stdin closed; envelope exiting")
    return 0
