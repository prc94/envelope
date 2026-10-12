"""Client for the model gateway's admin interface.

The gateway may run on another host (an LXC), so everything goes through a transport that
carries one JSON request line and, optionally, a byte stream after it: a local Unix socket,
or a command such as `ssh gateway-host relay` whose stdin and stdout reach the admin socket.
"""

from __future__ import annotations

import json
import socket
import subprocess
import tarfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Protocol

Stream = Callable[[IO[bytes]], None]


class GatewayLike(Protocol):
    """What the trial lifecycle needs from the gateway."""

    def issue(
        self,
        *,
        lane: str,
        job: str,
        trial: str,
        allowed_models: list[str],
        request_cap: int,
        auxiliary_models: list[str],
    ) -> str: ...
    def revoke(self, token: str) -> None: ...
    def state(self, token: str) -> dict[str, Any]: ...
    def ledger(self, trial: str) -> list[dict[str, Any]]: ...


class Transport(Protocol):
    def call(self, request: dict[str, Any], stream: Stream | None = None) -> dict[str, Any]: ...


class UnixTransport:
    def __init__(self, path: str | Path, timeout: float = 30) -> None:
        self.path, self.timeout = str(path), timeout

    def call(self, request: dict[str, Any], stream: Stream | None = None) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            with sock.makefile("wb") as out:
                out.write(json.dumps(request).encode() + b"\n")
                if stream is not None:
                    stream(out)
            sock.shutdown(socket.SHUT_WR)
            return json.loads(sock.makefile("rb").readline())


class CommandTransport:
    """Run a command whose stdin/stdout are wired to the admin socket (usually over ssh)."""

    def __init__(self, argv: list[str], timeout: float = 120) -> None:
        self.argv, self.timeout = argv, timeout

    def call(self, request: dict[str, Any], stream: Stream | None = None) -> dict[str, Any]:
        with subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as proc:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(json.dumps(request).encode() + b"\n")
                if stream is not None:
                    stream(proc.stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # the reply (or the error text on stderr) says why
            reply = proc.stdout.readline()
            proc.wait(timeout=self.timeout)
            if not reply:
                detail = (
                    proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
                )
                raise RuntimeError(
                    f"gateway relay gave no reply (exit {proc.returncode}): {detail}"
                )
            return json.loads(reply)


def ssh_transport(
    host: str, user: str, key: str | Path, known_hosts: str | Path, port: int = 22
) -> CommandTransport:
    """The runner's key on the gateway host is restricted to the `relay` forced command, and
    the host key is pinned (it was read from the container by whoever provisioned it)."""
    return CommandTransport(
        [
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            f"{user}@{host}",
            "relay",  # ignored: the forced command decides what runs
        ]
    )


DEFAULT_CONFIG = Path.home() / ".config" / "envelope-eval" / "gateway.toml"


def client_from_config(path: str | Path = DEFAULT_CONFIG) -> GatewayClient:
    """Client for the gateway described by the file the deployment script writes."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if "socket" in data:
        return GatewayClient.unix(data["socket"])
    return GatewayClient(
        ssh_transport(data["host"], data["user"], data["key"], data["known_hosts"])
    )


class GatewayClient:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    @classmethod
    def unix(cls, admin_socket: str | Path) -> GatewayClient:
        return cls(UnixTransport(admin_socket))

    def _call(self, stream: Stream | None = None, /, **request: Any) -> dict[str, Any]:
        reply = self.transport.call(request, stream)
        if "error" in reply:
            raise RuntimeError(f"gateway {request.get('op')}: {reply['error']}")
        return reply

    def issue(
        self,
        *,
        lane: str,
        job: str,
        trial: str,
        allowed_models: list[str],
        request_cap: int,
        auxiliary_models: list[str],
    ) -> str:
        reply = self._call(
            None,
            op="issue",
            lane=lane,
            job=job,
            trial=trial,
            allowed_models=allowed_models,
            request_cap=request_cap,
            auxiliary_models=auxiliary_models,
        )
        return reply["token"]

    def revoke(self, token: str) -> None:
        self._call(None, op="revoke", token=token)

    def models(self, check: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
        """Models the provider key may use (its guardrail applies); `check` splits a list into
        `allowed` and `blocked`."""
        return self._call(None, op="models", check=check or [], refresh=refresh)

    def state(self, token: str) -> dict[str, Any]:
        return self._call(None, op="state", token=token)

    def ledger(self, trial: str) -> list[dict[str, Any]]:
        return self._call(None, op="ledger", trial=trial)["records"]

    def scan(self, root: str | Path) -> list[dict[str, Any]]:
        """Credential scan by the gateway, which alone knows the key: `root` is sent as a tar
        stream, and findings name files relative to it."""

        def send(out: IO[bytes]) -> None:
            with tarfile.open(fileobj=out, mode="w|") as archive:
                archive.add(str(root), arcname=".")

        return self._call(send, op="scan_tar")["findings"]
