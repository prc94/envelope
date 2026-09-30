from pathlib import Path

from _lib.gen import rng_for, token


def generate(out, seed, prefix, db):
    rng = rng_for(seed, "redis")
    creds = db.redis_user(prefix)

    n_active = rng.randint(15, 40)
    n_other = rng.randint(20, 60)
    pairs: list[str] = []
    for _ in range(n_active):
        pairs += [f"{prefix}:session:{token(rng, 10)}", "active"]
    for _ in range(n_other):
        status = rng.choice(["expired", "pending", "revoked"])
        pairs += [f"{prefix}:session:{token(rng, 10)}", status]
    # noise outside the queried pattern, still inside the user's own key namespace
    for _ in range(rng.randint(10, 30)):
        pairs += [f"{prefix}:cache:{token(rng, 10)}", "active"]

    rc, _, err = db.ssh.run(
        [
            "docker", "exec", db.redis_container,
            "redis-cli", "--user", creds.user, "-a", creds.password, "--no-auth-warning",
            "MSET", *pairs,
        ],
        timeout=60,
    )  # fmt: skip
    if rc:
        raise RuntimeError(f"seeding redis fixture failed: {err.decode('utf-8', 'replace')}")

    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "redis.env").write_text(
        f"REDIS_HOST={creds.host}\n"
        f"REDIS_PORT={creds.port}\n"
        f"REDIS_USER={creds.user}\n"
        f"REDIS_PASSWORD={creds.password}\n",
        encoding="utf-8",
    )
    return {
        "expect": str(n_active),
        "params": {"key_prefix": prefix},
        "redact": [creds.password],
        "db_cleanup": [{"kind": "redis", "prefix": prefix}],
    }
