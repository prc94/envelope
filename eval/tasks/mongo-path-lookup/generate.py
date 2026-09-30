import json
from pathlib import Path

from _lib.gen import WORDS, rng_for, token

TUNNEL_HOST, TUNNEL_PORT = "127.0.0.1", 17017


def _path(rng):
    return "/" + "/".join(rng.choice(WORDS) for _ in range(rng.randint(2, 4)))


def generate(out, seed, prefix, db):
    rng = rng_for(seed, "mongo-path")
    creds = db.mongo_user(prefix)

    paths = set()
    while len(paths) < rng.randint(40, 70):
        paths.add(_path(rng))
    paths = sorted(paths)
    target = rng.choice(paths)

    docs, expect = [], None
    for path in paths:
        body = token(rng, 12)
        if path == target:
            expect = body
        docs.append({"path": path, "body": body})

    script = (
        f"db.getSiblingDB({json.dumps(creds.database)}).responses.insertMany({json.dumps(docs)})"
    )
    rc, _, err = db.ssh.run(
        [
            "docker", "exec", db.mongo_container,
            "mongosh", "--quiet", "-u", creds.user, "-p", creds.password,
            "--authenticationDatabase", creds.database, "--eval", script,
        ],
        timeout=60,
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
        "expect": expect,
        "params": {"key_path": target},
        "redact": [creds.password],
        "db_cleanup": [{"kind": "mongo", "prefix": prefix}],
    }
