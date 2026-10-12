"""A real host reached over SSH (the Windows test VM now, macOS later)."""

from __future__ import annotations

import io
import ipaddress
import json
import posixpath
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Fingerprint

FINGERPRINT_SCRIPT = Path(__file__).parents[3] / "launcher" / "fingerprint.py"


# Run on the target with `python -c`; the argument is the path. Kept to one line for cmd.
UNTAR = (
    "import os,sys,tarfile;d=sys.argv[1];os.makedirs(d,exist_ok=True);"
    "tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall(d,filter='data')"
)
TAR = (
    "import sys,tarfile;t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|',format=tarfile.PAX_FORMAT);"
    "t.add(sys.argv[1],arcname='.');t.close()"
)
KIND = (
    "import os,sys;p=sys.argv[1];"
    "print('dir' if os.path.isdir(p) else 'file') if os.path.exists(p) else sys.exit(3)"
)


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


class TunnelError(RuntimeError):
    pass


@dataclass
class Tunnel:
    process: subprocess.Popen[bytes]
    local_port: int
    target_port: int
    bind: str


class SSHTarget:
    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        *,
        windows: bool = True,
        python: str = "python",
        key: str | Path | None = None,
        known_hosts: str | Path | None = None,
        tools: tuple[str, ...] = (),
        connect_timeout: int = 10,
    ) -> None:
        self.host, self.user, self.port, self.windows = host, user, port, windows
        self.python, self.tools = python, tools
        self.key = Path(key).expanduser() if key else None
        self.known_hosts = Path(known_hosts).expanduser() if known_hosts else None
        self.connect_timeout = connect_timeout
        self._tunnels: list[Tunnel] = []
        # Seams for tests; production uses the real thing.
        self._run: Any = subprocess.run
        self._popen: Any = subprocess.Popen

    # -- command lines ---------------------------------------------------------------------

    def _options(self) -> list[str]:
        options = [
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.key:
            options += ["-i", str(self.key), "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            options += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
            options += ["-o", "StrictHostKeyChecking=yes"]
        return options

    def _ssh(self) -> list[str]:
        return ["ssh", *self._options(), "-p", str(self.port), f"{self.user}@{self.host}"]

    def _scp(self) -> list[str]:
        return ["scp", "-q", *self._options(), "-P", str(self.port)]

    def _remote(self, path: str) -> str:
        """scp remote spec; Windows paths use forward slashes (`C:/dir`), which scp accepts."""
        return f"{self.user}@{self.host}:{path.replace(chr(92), '/')}"

    def _command(self, argv: list[str]) -> str:
        return subprocess.list2cmdline(argv) if self.windows else shlex.join(argv)

    # -- commands and copies ---------------------------------------------------------------

    def run(
        self, argv: list[str], timeout: float | None = None, input: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        proc = self._run(
            [*self._ssh(), self._command(argv)],
            input=input,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run_shell(self, command: str, timeout: float | None = None) -> tuple[int, bytes, bytes]:
        """Run a command string as the remote login shell (cmd on Windows) sees it, for lines
        that need cmd's own quoting (`cd /d "C:\\a b" && ...`)."""
        proc = self._run([*self._ssh(), command], capture_output=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def put(self, source: Path, destination: str) -> None:
        """Copy a file, or a directory tree, byte for byte.

        A tree is sent as a tar stream that Python on the target extracts into `destination`
        (created if needed, merged if it exists): scp's directory rules differ between hosts (on
        the Windows VM a directory of plain files landed flat in the parent) and it cannot be
        trusted with non-ASCII names, while tar keeps names as UTF-8 and the target is explicit."""
        if source.is_dir():
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
                archive.add(str(source), arcname=".")
            rc, _, err = self.run(
                [self.python, "-c", UNTAR, destination], input=buffer.getvalue(), timeout=900
            )
            if rc:
                raise RuntimeError(f"upload to {destination} failed ({rc}): {_text(err)}")
        else:
            self._copy([*self._scp(), str(source), self._remote(destination)])

    def get(self, source: str, destination: Path) -> None:
        """Copy a file or tree from the target; raises FileNotFoundError if it is absent."""
        if not self._is_tree(source):
            with tempfile.TemporaryDirectory() as tmp:
                self._copy([*self._scp(), self._remote(source), tmp])
                fetched = Path(tmp) / posixpath.basename(source.replace("\\", "/").rstrip("/"))
                shutil.copyfile(fetched, destination)
            return
        rc, out, err = self.run([self.python, "-c", TAR, source], timeout=900)
        if rc:
            if "No such file" in _text(err) or "cannot find" in _text(err).lower():
                raise FileNotFoundError(source)
            raise RuntimeError(f"download of {source} failed ({rc}): {_text(err)}")
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(out), mode="r") as archive:
            # Windows device names (notably NUL) can appear in a workspace tar as character
            # devices. They are not workspace files; the data filter still protects every
            # extracted member from links or paths outside the destination.
            members = (member for member in archive if not (self.windows and member.isdev()))
            archive.extractall(destination, members=members, filter="data")

    def _is_tree(self, remote: str) -> bool:
        rc, out, _ = self.run([self.python, "-c", KIND, remote], timeout=60)
        if rc == 3:
            raise FileNotFoundError(remote)
        return rc == 0 and out.strip() == b"dir"

    def _copy(self, argv: list[str]) -> None:
        proc = self._run(argv, capture_output=True, check=False)
        if proc.returncode:
            text = proc.stderr.decode("utf-8", "replace").strip()
            if "No such file" in text or "not found" in text.lower():
                raise FileNotFoundError(text)
            raise RuntimeError(f"scp failed ({proc.returncode}): {text}")

    # -- reverse tunnels -------------------------------------------------------------------

    def probe_port(self, port: int, bind: str = "127.0.0.1") -> bool:
        """Whether something listens on `bind:port` on the target."""
        code = (
            f"import socket; socket.create_connection(({bind!r}, {port}), 5).close(); print('up')"
        )
        try:
            rc, out, _ = self.run([self.python, "-c", code], timeout=60)
        except subprocess.TimeoutExpired:
            return False
        return rc == 0 and b"up" in out

    def open_reverse_tunnel(
        self, local_port: int, target_port: int, bind: str = "127.0.0.1", wait: float = 15.0
    ) -> Tunnel:
        """Forward target `bind:target_port` to this host's `local_port` (gateway, databases).

        Bound to loopback on the target, or, for a Docker target whose trial containers run on
        an `internal` network and so cannot reach the host's own loopback, to that network's
        bridge gateway address instead (the same loopback-or-private-bridge rule the gateway
        itself enforces on its own bind address); failing at setup if the forward cannot be made."""
        ip = ipaddress.ip_address(bind)
        if ip.is_unspecified or not (ip.is_loopback or ip.is_private):
            raise ValueError(
                "reverse tunnels must be bound to loopback or a private bridge address"
            )
        argv = [
            *self._ssh()[:-1],
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-R",
            f"{bind}:{target_port}:127.0.0.1:{local_port}",
            self._ssh()[-1],
        ]
        proc = self._popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        tunnel = Tunnel(proc, local_port, target_port, bind)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = (
                    proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
                )
                raise TunnelError(
                    f"tunnel to port {target_port} failed: {detail or proc.returncode}"
                )
            if self.probe_port(target_port, bind):
                self._tunnels.append(tunnel)
                return tunnel
            time.sleep(0.3)
        proc.kill()
        raise TunnelError(f"tunnel to port {target_port} did not come up within {wait:.0f}s")

    def tunnel_health(self) -> list[str]:
        """Problems with the open tunnels (empty when healthy): the ssh process must be alive
        and the forwarded port must accept connections on the target."""
        problems = []
        for tunnel in self._tunnels:
            if tunnel.process.poll() is not None:
                problems.append(
                    f"tunnel {tunnel.target_port}: ssh exited ({tunnel.process.returncode})"
                )
            elif not self.probe_port(tunnel.target_port, tunnel.bind):
                problems.append(f"tunnel {tunnel.target_port}: nothing listening on the target")
        return problems

    def restore_tunnels(self) -> None:
        """Re-open tunnels that are down (the job is paused until this succeeds)."""
        for tunnel in list(self._tunnels):
            if tunnel.process.poll() is None and self.probe_port(tunnel.target_port, tunnel.bind):
                continue
            if tunnel.process.poll() is None:
                tunnel.process.kill()
            self._tunnels.remove(tunnel)
            self.open_reverse_tunnel(tunnel.local_port, tunnel.target_port, tunnel.bind)

    def close_tunnels(self) -> None:
        for tunnel in self._tunnels:
            if tunnel.process.poll() is None:
                tunnel.process.terminate()
        self._tunnels.clear()

    # -- fingerprint -----------------------------------------------------------------------

    def fingerprint(self) -> Fingerprint:
        """Measure the target with launcher/fingerprint.py streamed to `python -` (no upload)."""
        args = [arg for tool in self.tools for arg in ("--tool", tool)]
        rc, out, err = self.run(
            [self.python, "-", *args], input=FINGERPRINT_SCRIPT.read_bytes(), timeout=180
        )
        if rc:
            raise RuntimeError(f"fingerprint probe failed ({rc}): {err.decode('utf-8', 'replace')}")
        return Fingerprint(json.loads(out))
