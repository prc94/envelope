"""Stdio transport: a reader thread feeding an asyncio queue, and a locked writer.

A thread (rather than asyncio pipe transports) keeps behavior identical on
Windows, where the Proactor loop cannot read an inherited console/pipe stdin
reliably.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import BinaryIO, Protocol


class LineSource(Protocol):
    async def readline(self) -> bytes | None:
        """Next line without the newline, or None at end of input."""
        ...


class ThreadedLineSource:
    def __init__(self, stream: BinaryIO, loop: asyncio.AbstractEventLoop) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._loop = loop
        self._stream = stream
        threading.Thread(target=self._pump, name="envelope-stdin", daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in self._stream:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, line.rstrip(b"\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            except RuntimeError:
                pass  # loop already closed

    async def readline(self) -> bytes | None:
        return await self._queue.get()


class QueueLineSource:
    """In-memory source for tests."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def feed(self, line: bytes | None) -> None:
        self.queue.put_nowait(line)

    async def readline(self) -> bytes | None:
        return await self.queue.get()


Writer = Callable[[bytes], None]


def stream_writer(stream: BinaryIO) -> Writer:
    lock = threading.Lock()

    def write(data: bytes) -> None:
        with lock:
            stream.write(data)
            stream.flush()

    return write
