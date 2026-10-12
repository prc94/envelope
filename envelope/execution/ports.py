"""Tri-state listening-port lookup, filtered to a command's process tree
(design B5). ``listening_ports()`` returns ``None`` where envelope has no
verified way to attribute socket ownership (macOS, a Linux `hidepid` mount,
or any lookup failure) -- callers must treat that as "unknown", never as
"not listening", and fall back to ``port_reachable()`` instead.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

_LISTEN_STATE = "0A"


def _linux_listening_inodes() -> dict[str, int]:
    """Map each LISTEN-state socket's inode to its local port."""
    inodes: dict[str, int] = {}
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _LISTEN_STATE:
                continue
            _, _, port_hex = fields[1].rpartition(":")
            try:
                port = int(port_hex, 16)
            except ValueError:
                continue
            inodes[fields[9]] = port
    return inodes


def _linux_tree_ports(pids: frozenset[int]) -> frozenset[int]:
    listening = _linux_listening_inodes()
    if not listening:
        return frozenset()
    ports: set[int] = set()
    for pid in pids:
        try:
            entries = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue  # the pid exited, or (not our own process) is denied -- skip it
        for entry in entries:
            try:
                target = entry.readlink().as_posix()
            except OSError:
                continue
            if target.startswith("socket:["):
                inode = target[len("socket:[") : -1]
                if inode in listening:
                    ports.add(listening[inode])
    return frozenset(ports)


def listening_ports(pids: frozenset[int]) -> frozenset[int] | None:
    """Ports this command's tree listens on, or ``None`` where ownership
    cannot be verified on this platform."""
    if sys.platform.startswith("linux"):
        return _linux_tree_ports(pids)
    if sys.platform == "win32":
        from envelope.execution.windows import listening_ports_in_pids

        return listening_ports_in_pids(pids)
    return None  # macOS: documented limitation (design risk list)


def listening_owner(port: int) -> tuple[int, str] | None:
    """Best-effort owner of a listening port; unknown on inaccessible hosts."""
    if not sys.platform.startswith("linux"):
        return None
    inodes = {inode for inode, value in _linux_listening_inodes().items() if value == port}
    if not inodes:
        return None
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.readlink().as_posix()
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                try:
                    name = (process / "comm").read_text().strip()
                except OSError:
                    name = "process"
                return int(process.name), name
    return None


def port_reachable(port: int, *, timeout: float = 0.2) -> bool:
    """Unverified fallback (design B5): used only where ``listening_ports()``
    returned ``None``, so the result states that ownership was not verified.
    ``create_connection`` already tries every address `getaddrinfo` resolves
    "localhost" to (both address families), succeeding on the first that
    connects."""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False
