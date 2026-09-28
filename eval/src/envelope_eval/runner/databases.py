"""Administration of the shared MongoDB and Redis servers the Linux lane's database tasks use
(design.md's database-server note; evaluation-runner spec "Secrets in artifacts"): per-trial ACL
users and databases that generators create, deleted at trial cleanup, and prefix garbage
collection at job start and end so an interrupted trial's objects do not linger.

Both servers are long-lived containers on the same `docker` host as the trial containers (not
started per trial), reached two ways: by container name on the trial network (a trial container
resolves them through Docker's embedded DNS, the same reasoning as the model gateway's Docker
bridge reachability) for the harness itself, and admin operations run here over `docker exec`
through the SSH connection that also drives the `docker` lane's trial containers — never through
a trial's own network, which has no route to the admin surface. Both are published on the host's
own loopback too, for the Windows lane's reverse tunnels later and for a plain SSH `-L` forward
now (evaluation-runner spec: "Every server the evaluation starts ... SHALL listen only on
loopback or the Docker bridge address of the runner host").
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Protocol

REDIS_IMAGE = "redis:8"
MONGO_IMAGE = "mongo:8"


class DatabaseError(RuntimeError):
    pass


class AdminTransport(Protocol):
    """What `DatabaseAdmin` needs to reach the `docker` host: `SSHTarget.run` satisfies this in
    production; tests use a fake."""

    def run(self, argv: list[str], timeout: float | None = None) -> tuple[int, bytes, bytes]: ...


@dataclass(frozen=True)
class RedisCredentials:
    host: str
    port: int
    user: str
    password: str
    key_prefix: str  # the user may only touch keys matching "<key_prefix>:*"


@dataclass(frozen=True)
class MongoCredentials:
    host: str
    port: int
    database: str
    user: str
    password: str


class DatabaseAdmin:
    """Admin connection to the shared servers. A generator calls `redis_user`/`mongo_user` on
    the runner host during fixture generation; `TrialExecutor` calls `redis_delete`/`mongo_delete`
    at trial cleanup and `gc` at job start and end."""

    def __init__(
        self,
        ssh: AdminTransport,
        redis_admin_password: str,
        mongo_admin_user: str,
        mongo_admin_password: str,
        redis_container: str = "envelope-eval-redis",
        mongo_container: str = "envelope-eval-mongo",
        redis_port: int = 6379,
        mongo_port: int = 27017,
    ) -> None:
        self.ssh = ssh
        self._redis_pw = redis_admin_password
        self._mongo_user, self._mongo_pw = mongo_admin_user, mongo_admin_password
        self.redis_container, self.mongo_container = redis_container, mongo_container
        self.redis_port, self.mongo_port = redis_port, mongo_port

    # -- provisioning the servers themselves (not per trial) ---------------------------------

    def start(
        self,
        network: str,
        redis_local_port: int = 16379,
        mongo_local_port: int = 17017,
        ready_timeout: float = 30.0,
    ) -> None:
        """Start both servers if not already running, published on the host's own loopback, and
        also joined to `network` (the trial containers' own network, so a trial container reaches
        them by container name). Idempotent: an existing, running container is left alone.

        The two are separate `docker run`/`docker network connect` steps, not one `--network` at
        creation: `network` is created `--internal` (no route beyond the Docker host at all, by
        design — see the `docker` target), and Docker silently drops `-p` publishing for a
        container's *creation* network when that network is internal, even though the container
        can still be reached from the host once a second, non-internal network membership exists.
        A freshly started container does not accept authenticated connections the instant
        `docker run` returns (Redis needs a moment; Mongo's entrypoint creates the root user in
        the background before enabling auth), so a fresh start additionally waits for it."""
        if self._ensure(
            self.redis_container,
            [
                "docker", "run", "-d", "--name", self.redis_container,
                "-p", f"127.0.0.1:{redis_local_port}:{self.redis_port}",
                REDIS_IMAGE, "redis-server", "--requirepass", self._redis_pw,
            ],
            network,
        ):  # fmt: skip
            self._wait_redis_ready(ready_timeout)
        if self._ensure(
            self.mongo_container,
            [
                "docker", "run", "-d", "--name", self.mongo_container,
                "-p", f"127.0.0.1:{mongo_local_port}:{self.mongo_port}",
                "-e", f"MONGO_INITDB_ROOT_USERNAME={self._mongo_user}",
                "-e", f"MONGO_INITDB_ROOT_PASSWORD={self._mongo_pw}",
                MONGO_IMAGE,
            ],
            network,
        ):  # fmt: skip
            self._wait_mongo_ready(ready_timeout * 2)  # Mongo's own init takes longer than Redis's

    def _ensure(self, name: str, run_argv: list[str], network: str) -> bool:
        """Returns whether a fresh container was started (False: an existing one was already
        running and left alone)."""
        rc, out, _ = self.ssh.run(["docker", "inspect", "-f", "{{.State.Running}}", name])
        if rc == 0 and out.strip() == b"true":
            return False
        self.ssh.run(["docker", "rm", "-f", name])  # a stopped/stale container of the same name
        rc, _, err = self.ssh.run(run_argv, timeout=60)
        if rc:
            raise DatabaseError(f"starting {name} failed: {err.decode('utf-8', 'replace')}")
        rc, _, err = self.ssh.run(["docker", "network", "connect", network, name], timeout=30)
        if rc:
            raise DatabaseError(
                f"joining {name} to network {network!r} failed: {err.decode('utf-8', 'replace')}"
            )
        return True

    def _wait_redis_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, out, _ = self._redis_cli("PING")
            if rc == 0 and b"PONG" in out:
                return
            time.sleep(0.5)
        raise DatabaseError(f"redis did not accept authenticated connections within {timeout:.0f}s")

    def _wait_mongo_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, out, _ = self._mongosh("db.adminCommand({ping: 1}).ok")
            if rc == 0 and out.strip() == b"1":
                return
            time.sleep(1.0)
        raise DatabaseError(f"mongo did not accept authenticated connections within {timeout:.0f}s")

    # -- redis ---------------------------------------------------------------------------------

    def _redis_cli(self, *args: str, timeout: float = 30) -> tuple[int, bytes, bytes]:
        return self.ssh.run(
            [
                "docker", "exec", self.redis_container,
                "redis-cli", "-a", self._redis_pw, "--no-auth-warning", *args,
            ],
            timeout=timeout,
        )  # fmt: skip

    def redis_user(self, prefix: str) -> RedisCredentials:
        """A user limited to keys under `<prefix>:*`, every command but the dangerous ones
        (`FLUSHALL`, `CONFIG`, `SHUTDOWN`, `KEYS`, ...)."""
        password = secrets.token_hex(16)
        rc, _, err = self._redis_cli(
            "ACL", "SETUSER", prefix, "reset", "on", f">{password}",
            f"~{prefix}:*", "+@all", "-@dangerous", "resetchannels",
        )  # fmt: skip
        if rc:
            raise DatabaseError(f"redis ACL SETUSER {prefix}: {err.decode('utf-8', 'replace')}")
        return RedisCredentials(self.redis_container, self.redis_port, prefix, password, prefix)

    def redis_delete(self, prefix: str) -> None:
        rc, out, _ = self._redis_cli("--scan", "--pattern", f"{prefix}:*")
        if rc == 0:
            keys = [k for k in out.decode("utf-8", "replace").splitlines() if k]
            if keys:
                self._redis_cli("DEL", *keys)
        self._redis_cli("ACL", "DELUSER", prefix)

    def redis_gc(self, job_prefix: str) -> list[str]:
        rc, out, err = self._redis_cli("ACL", "LIST")
        if rc:
            raise DatabaseError(f"redis ACL LIST: {err.decode('utf-8', 'replace')}")
        removed = []
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split()
            name = parts[1] if len(parts) > 1 and parts[0] == "user" else ""
            if name.startswith(f"{job_prefix}-"):
                self.redis_delete(name)
                removed.append(name)
        return removed

    # -- mongo ---------------------------------------------------------------------------------

    def _mongosh(self, script: str, timeout: float = 30) -> tuple[int, bytes, bytes]:
        return self.ssh.run(
            [
                "docker", "exec", self.mongo_container,
                "mongosh", "--quiet", "-u", self._mongo_user, "-p", self._mongo_pw,
                "--authenticationDatabase", "admin", "--eval", script,
            ],
            timeout=timeout,
        )  # fmt: skip

    def mongo_user(self, prefix: str) -> MongoCredentials:
        """A database named `prefix` and a user with `readWrite` on it only."""
        password = secrets.token_hex(16)
        script = (
            f"db.getSiblingDB({prefix!r}).createUser("
            f"{{user: {prefix!r}, pwd: {password!r}, "
            f"roles: [{{role: 'readWrite', db: {prefix!r}}}]}})"
        )
        rc, _, err = self._mongosh(script)
        if rc:
            raise DatabaseError(f"mongo createUser {prefix}: {err.decode('utf-8', 'replace')}")
        return MongoCredentials(self.mongo_container, self.mongo_port, prefix, prefix, password)

    def mongo_delete(self, prefix: str) -> None:
        # dropDatabase() alone does not remove the database's own users (Mongo stores user
        # credentials independently of the data); both must go.
        self._mongosh(f"db.getSiblingDB({prefix!r}).dropAllUsers()")
        self._mongosh(f"db.getSiblingDB({prefix!r}).dropDatabase()")

    def mongo_gc(self, job_prefix: str) -> list[str]:
        """Enumerates by user, not `listDatabases`: `createUser` does not make Mongo create the
        database itself until something writes to it, so a trial whose generator provisioned a
        user but was interrupted before writing any data would otherwise be invisible."""
        rc, out, err = self._mongosh(
            "db.getSiblingDB('admin').system.users.find({}, {db: 1, _id: 0})"
            ".toArray().map(u => u.db).join('\\n')"
        )
        if rc:
            raise DatabaseError(f"mongo system.users: {err.decode('utf-8', 'replace')}")
        removed = []
        for name in out.decode("utf-8", "replace").splitlines():
            name = name.strip()
            if name.startswith(f"{job_prefix}-") and name not in removed:
                self.mongo_delete(name)
                removed.append(name)
        return removed

    # -- job-level GC ---------------------------------------------------------------------------

    def gc(self, job_prefix: str) -> dict[str, list[str]]:
        """Remove every Redis user and Mongo database left under `job_prefix`: called at job
        start (cleaning up anything an earlier interrupted run of the same job name left behind)
        and again at job end (a final sweep)."""
        return {"redis": self.redis_gc(job_prefix), "mongo": self.mongo_gc(job_prefix)}
