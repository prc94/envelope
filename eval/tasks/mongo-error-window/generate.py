import json
from pathlib import Path

from _lib.gen import rng_for

# The agent runs on the Windows VM, which reaches Mongo/Redis through the reverse-tunnel chain
# docs/windows-test-vm.md records (this host's loopback -> envelope-linux's published container
# port -> a reverse tunnel into the VM's own loopback), not by container name as the Linux lane
# does (task 13.1's mongo-customer-total): the fixture must give the VM the tunnel's own address.
TUNNEL_HOST, TUNNEL_PORT = "127.0.0.1", 17017


def generate(out, seed, prefix, db):
    rng = rng_for(seed, "mongo-win")
    creds = db.mongo_user(prefix)

    base = 1_700_000_000
    n = rng.randint(2200, 3200)
    window_start_i = rng.randint(300, n - 900)
    window = rng.randint(150, 400)
    start, end = base + window_start_i * 5, base + (window_start_i + window) * 5

    docs, expect = [], 0
    other_levels = ["INFO", "DEBUG", "WARN"]
    for i in range(n):
        ts = base + i * 5
        level = "ERROR" if rng.random() < 0.08 else rng.choice(other_levels)
        if level == "ERROR" and start <= ts <= end:
            expect += 1
        docs.append({"ts": ts, "level": level, "msg": f"event {i}"})

    # The insertMany script travels on stdin, not as an `--eval` argv element: at this size (a few
    # thousand documents) an inline `--eval` string blows past the local ssh child's own argv
    # limit ([Errno 7] Argument list too long) well before it reaches the remote command line.
    # mongosh reads a script from stdin when none is given and stdin is not a tty.
    script = f"db.getSiblingDB({json.dumps(creds.database)}).events.insertMany({json.dumps(docs)})"
    rc, _, err = db.ssh.run(
        [
            "docker", "exec", "-i", db.mongo_container,
            "mongosh", "--quiet", "-u", creds.user, "-p", creds.password,
            "--authenticationDatabase", creds.database,
        ],
        input=script.encode("utf-8"),
        timeout=90,
    )  # fmt: skip
    if rc:
        raise RuntimeError(f"seeding mongo fixture failed: {err.decode('utf-8', 'replace')}")

    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "mongo.env").write_text(
        f"MONGO_HOST={TUNNEL_HOST}\n"
        f"MONGO_PORT={TUNNEL_PORT}\n"
        f"MONGO_DB={creds.database}\n"
        f"MONGO_USER={creds.user}\n"
        f"MONGO_PASSWORD={creds.password}\n",
        encoding="utf-8",
    )
    return {
        "expect": str(expect),
        "params": {"start": start, "end": end},
        "redact": [creds.password],
        "db_cleanup": [{"kind": "mongo", "prefix": prefix}],
    }
