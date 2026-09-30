"""DatabaseAdmin (task 12.1): per-trial Redis ACL users and Mongo users/databases, deletion and
prefix garbage collection, against a fake `docker exec`/`docker run` transport."""

from __future__ import annotations

from collections.abc import Callable

from envelope_eval.runner.databases import DatabaseAdmin, DatabaseError

Handler = Callable[[list[str]], tuple[int, bytes, bytes]]


class FakeSSH:
    def __init__(self, handler: Handler | None = None):
        self.calls: list[list[str]] = []
        self.handler = handler or (lambda argv: (0, b"", b""))

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        return self.handler(list(argv))


def admin(handler: Handler | None = None) -> tuple[DatabaseAdmin, FakeSSH]:
    ssh = FakeSSH(handler)
    return (
        DatabaseAdmin(ssh, redis_admin_password="rpw", mongo_admin_user="root", mongo_admin_password="mpw"),
        ssh,
    )  # fmt: skip


# -- server provisioning ---------------------------------------------------------------------


def test_start_skips_a_container_already_running():
    def handler(argv):
        if argv[:3] == ["docker", "inspect", "-f"]:
            return 0, b"true\n", b""
        raise AssertionError(f"should not run: {argv}")

    a, ssh = admin(handler)
    a.start("envelope-eval")
    kinds = [c[:2] for c in ssh.calls]
    assert kinds.count(["docker", "run"]) == 0


def test_start_runs_both_containers_when_absent_with_the_right_network_and_credentials():
    def handler(argv):
        if argv[:3] == ["docker", "inspect", "-f"]:
            return 1, b"", b"no such container"
        if "PING" in argv:
            return 0, b"PONG\n", b""
        if "ping" in " ".join(argv):
            return 0, b"1\n", b""
        return 0, b"", b""

    a, ssh = admin(handler)
    a.start("envelope-eval", redis_local_port=16379, mongo_local_port=17017)
    runs = [c for c in ssh.calls if c[:2] == ["docker", "run"]]
    assert len(runs) == 2
    redis_run, mongo_run = runs
    # not on --network at creation: `docker run --network <internal> -p ...` silently drops the
    # publish for an internal network, so the containers join it as a second step instead
    assert "--network" not in redis_run and "--network" not in mongo_run
    assert "127.0.0.1:16379:6379" in redis_run
    assert "--requirepass" in redis_run and "rpw" in redis_run
    assert "127.0.0.1:17017:27017" in mongo_run
    assert "MONGO_INITDB_ROOT_USERNAME=root" in mongo_run
    assert "MONGO_INITDB_ROOT_PASSWORD=mpw" in mongo_run
    # a stale container of the same name is removed first
    removals = [c for c in ssh.calls if c[:3] == ["docker", "rm", "-f"]]
    assert len(removals) == 2
    connects = [c for c in ssh.calls if c[:3] == ["docker", "network", "connect"]]
    assert connects == [
        ["docker", "network", "connect", "envelope-eval", "envelope-eval-redis"],
        ["docker", "network", "connect", "envelope-eval", "envelope-eval-mongo"],
    ]


def test_start_raises_if_joining_the_trial_network_fails():
    def handler(argv):
        if argv[:3] == ["docker", "inspect", "-f"]:
            return 1, b"", b"no such container"
        if argv[:3] == ["docker", "network", "connect"]:
            return 1, b"", b"no such network"
        return 0, b"", b""

    a, _ssh = admin(handler)
    try:
        a.start("envelope-eval")
    except DatabaseError as exc:
        assert "joining" in str(exc)
    else:
        raise AssertionError("expected DatabaseError")


def test_start_waits_for_a_freshly_started_redis_to_accept_authenticated_connections():
    calls_before_ready = {"n": 0}

    def handler(argv):
        if argv[:3] == ["docker", "inspect", "-f"]:
            return 1, b"", b"no such container"
        if "PING" in argv:
            calls_before_ready["n"] += 1
            return (0, b"PONG\n", b"") if calls_before_ready["n"] >= 3 else (1, b"", b"connecting")
        if "ping" in " ".join(argv):
            return 0, b"1\n", b""
        return 0, b"", b""

    a, _ssh = admin(handler)
    a.start("envelope-eval", ready_timeout=5.0)
    assert calls_before_ready["n"] == 3


def test_start_raises_if_a_fresh_container_never_becomes_ready():
    def handler(argv):
        if argv[:3] == ["docker", "inspect", "-f"]:
            return 1, b"", b"no such container"
        if "PING" in argv:
            return 1, b"", b"connection refused"
        return 0, b"", b""

    a, _ssh = admin(handler)
    try:
        a.start("envelope-eval", ready_timeout=0.05)
    except DatabaseError as exc:
        assert "did not accept authenticated connections" in str(exc)
    else:
        raise AssertionError("expected DatabaseError")


# -- redis -------------------------------------------------------------------------------------


def test_redis_user_scopes_the_acl_to_the_prefixs_own_keys_without_dangerous_commands():
    a, ssh = admin()
    creds = a.redis_user("job-t00001")
    setuser = next(c for c in ssh.calls if "SETUSER" in c)
    assert "job-t00001" in setuser
    assert "~job-t00001:*" in setuser
    assert "-@dangerous" in setuser
    assert creds.user == "job-t00001" and creds.key_prefix == "job-t00001"
    assert creds.password and creds.password != "rpw"


def test_redis_admin_password_is_the_requirepass_value():
    # The Windows lane's pinned redis-cli (5.0.14.1) predates Redis ACL and has no `--user` flag,
    # so Windows database tasks (13.4) authenticate with this instead of a per-trial ACL account.
    a, _ = admin()
    assert a.redis_admin_password() == "rpw"


def test_redis_delete_scans_and_deletes_matching_keys_then_the_user():
    def handler(argv):
        if "--scan" in argv:
            return 0, b"job-t00001:a\njob-t00001:b\n", b""
        return 0, b"", b""

    a, ssh = admin(handler)
    a.redis_delete("job-t00001")
    dels = [c for c in ssh.calls if "DEL" in c]
    assert dels and "job-t00001:a" in dels[0] and "job-t00001:b" in dels[0]
    assert any("DELUSER" in c and "job-t00001" in c for c in ssh.calls)


def test_redis_delete_skips_del_when_no_keys_matched():
    a, ssh = admin(lambda argv: (0, b"", b""))
    a.redis_delete("job-t00001")
    assert not any("DEL" in c for c in ssh.calls)


def test_redis_gc_only_removes_users_under_the_job_prefix():
    def handler(argv):
        if argv[-2:] == ["ACL", "LIST"]:
            return (
                0,
                b"user default on nopass ~* &* +@all\n"
                + b"user job1-t00001 on >x ~job1-t00001:* +@all -@dangerous\n"
                + b"user job2-t00001 on >x ~job2-t00001:* +@all -@dangerous\n",
                b"",
            )
        return 0, b"", b""

    a, ssh = admin(handler)
    removed = a.redis_gc("job1")
    assert removed == ["job1-t00001"]
    assert any("DELUSER" in c and "job1-t00001" in c for c in ssh.calls)
    assert not any("DELUSER" in c and "job2-t00001" in c for c in ssh.calls)
    assert not any("DELUSER" in c and "default" in c for c in ssh.calls)


# -- mongo -------------------------------------------------------------------------------------


def test_mongo_user_creates_a_readwrite_user_scoped_to_its_own_database():
    a, ssh = admin()
    creds = a.mongo_user("job-t00001")
    script = next(c for c in ssh.calls if "createUser" in " ".join(c))[-1]
    assert "job-t00001" in script and "readWrite" in script
    assert creds.database == "job-t00001" and creds.user == "job-t00001"
    assert creds.password


def test_mongo_delete_drops_users_before_the_database():
    a, ssh = admin()
    a.mongo_delete("job-t00001")
    scripts = [c[-1] for c in ssh.calls if c[:2] == ["docker", "exec"]]
    assert any("dropAllUsers" in s for s in scripts)
    assert any("dropDatabase" in s for s in scripts)
    users_idx = next(i for i, s in enumerate(scripts) if "dropAllUsers" in s)
    db_idx = next(i for i, s in enumerate(scripts) if "dropDatabase" in s)
    assert users_idx < db_idx


def test_mongo_gc_only_removes_databases_under_the_job_prefix():
    """Enumerated by user, not `listDatabases`: `createUser` alone does not make Mongo create
    the database, so a trial interrupted before writing any data still has to be found."""

    def handler(argv):
        if "system.users" in " ".join(argv):
            return 0, b"admin\njob1-t00001\njob2-t00001\n", b""
        return 0, b"", b""

    a, ssh = admin(handler)
    removed = a.mongo_gc("job1")
    assert removed == ["job1-t00001"]
    # the enumeration script itself legitimately reads admin.system.users; only the delete calls
    # (dropAllUsers/dropDatabase) must never target it
    deletes = " ".join(
        c[-1]
        for c in ssh.calls
        if c[:2] == ["docker", "exec"] and ("dropAllUsers" in c[-1] or "dropDatabase" in c[-1])
    )
    assert (
        "'job1-t00001'" in deletes and "'job2-t00001'" not in deletes and "'admin'" not in deletes
    )


# -- job-level GC --------------------------------------------------------------------------------


def test_gc_runs_both_redis_and_mongo_gc():
    def handler(argv):
        if argv[-2:] == ["ACL", "LIST"]:
            return 0, b"user job-t00001 on >x ~job-t00001:* +@all -@dangerous\n", b""
        if "system.users" in " ".join(argv):
            return 0, b"job-t00001\n", b""
        return 0, b"", b""

    a, _ssh = admin(handler)
    result = a.gc("job")
    assert result == {"redis": ["job-t00001"], "mongo": ["job-t00001"]}


def test_gc_removes_nothing_left_by_a_different_job():
    def handler(argv):
        if argv[-2:] == ["ACL", "LIST"]:
            return 0, b"user other-t00001 on >x ~other-t00001:* +@all -@dangerous\n", b""
        if "system.users" in " ".join(argv):
            return 0, b"other-t00001\n", b""
        return 0, b"", b""

    a, _ssh = admin(handler)
    assert a.gc("job") == {"redis": [], "mongo": []}
