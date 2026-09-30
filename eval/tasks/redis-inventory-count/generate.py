from pathlib import Path

from _lib.gen import rng_for, token

# The agent runs on the Windows VM and reaches the shared Redis container through the
# reverse-tunnel chain docs/windows-test-vm.md records, not by container name as the Linux lane
# does (see mongo-error-window/generate.py). Authentication also differs from the Linux lane's
# redis-session-count: the pinned Windows redis-cli (5.0.14.1) predates Redis ACL and has no
# `--user` flag, so this task authenticates with the server's shared `requirepass` value
# (`redis_admin_password()`) instead of a per-trial `redis_user()` account; Windows trials run
# serially on one VM, so the shared credential costs no real isolation (design.md, task 13.4).
TUNNEL_HOST, TUNNEL_PORT = "127.0.0.1", 16379


def generate(out, seed, prefix, db):
    rng = rng_for(seed, "redis-win")
    password = db.redis_admin_password()

    n_in_stock = rng.randint(15, 40)
    n_other = rng.randint(20, 60)
    pairs: list[str] = []
    for _ in range(n_in_stock):
        pairs += [f"{prefix}:item:{token(rng, 10)}", "in-stock"]
    for _ in range(n_other):
        status = rng.choice(["out-of-stock", "backordered", "discontinued"])
        pairs += [f"{prefix}:item:{token(rng, 10)}", status]

    rc, _, err = db.ssh.run(
        [
            "docker", "exec", db.redis_container,
            "redis-cli", "-a", password, "--no-auth-warning",
            "MSET", *pairs,
        ],
        timeout=60,
    )  # fmt: skip
    if rc:
        raise RuntimeError(f"seeding redis fixture failed: {err.decode('utf-8', 'replace')}")

    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "redis.env").write_text(
        f"REDIS_HOST={TUNNEL_HOST}\nREDIS_PORT={TUNNEL_PORT}\nREDIS_PASSWORD={password}\n",
        encoding="utf-8",
    )
    return {
        "expect": str(n_in_stock),
        "params": {"key_prefix": prefix},
        "redact": [password],
        "db_cleanup": [{"kind": "redis", "prefix": prefix}],
    }
