import json
from pathlib import Path

from _lib.gen import FIRST, LAST, rng_for


def generate(out, seed, prefix, db):
    rng = rng_for(seed, "mongo")
    creds = db.mongo_user(prefix)

    customers = sorted({f"{rng.choice(FIRST)}.{rng.choice(LAST)}" for _ in range(8)})
    while len(customers) < 5:
        customers.append(f"{rng.choice(FIRST)}.{rng.choice(LAST)}{rng.randint(0, 99)}")
    target = rng.choice(customers)

    docs, total_cents = [], 0
    for i in range(rng.randint(80, 160)):
        customer = rng.choice(customers)
        cents = rng.randint(500, 50000)
        if customer == target:
            total_cents += cents
        docs.append({"order_id": f"o{i:05d}", "customer": customer, "amount_cents": cents})

    script = f"db.getSiblingDB({json.dumps(creds.database)}).orders.insertMany({json.dumps(docs)})"
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
        f"MONGO_HOST={creds.host}\n"
        f"MONGO_PORT={creds.port}\n"
        f"MONGO_DB={creds.database}\n"
        f"MONGO_USER={creds.user}\n"
        f"MONGO_PASSWORD={creds.password}\n",
        encoding="utf-8",
    )
    return {
        "expect": f"{total_cents / 100:.2f}",
        "params": {"customer": target},
        "redact": [creds.password],
        "db_cleanup": [{"kind": "mongo", "prefix": prefix}],
    }
