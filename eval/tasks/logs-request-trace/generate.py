import json
from pathlib import Path

from _lib.gen import FIRST, rng_for, stamp, token, write_lines

PATHS = [
    "/api/orders",
    "/api/cart",
    "/api/search",
    "/api/profile",
    "/api/pay",
    "/api/items",
    "/login",
    "/logout",
    "/health",
    "/static/app.js",
    "/api/export",
    "/api/import",
]


def generate(out, seed):
    rng = rng_for(seed, "trace2")
    target_path = rng.choice(PATHS)
    users = [f"{f}.{rng.choice(FIRST)}" for f in FIRST] + FIRST
    nginx, app, t = [], [], 1_700_000_000
    bad = rng.randrange(8000)
    expect = None
    for i in range(8000):
        t += rng.randint(0, 2)
        rid, user = token(rng, 12), rng.choice(users)
        path = rng.choice(PATHS)
        status = rng.choices([200, 200, 200, 301, 404, 500], [60, 20, 10, 4, 4, 2])[0]
        if path == target_path and status == 500:
            status = 502  # the only 500 on the target path is the planted one
        if i == bad:
            path, status, expect = target_path, 500, user
        nginx.append(
            f'10.0.{rng.randint(0, 9)}.{rng.randint(1, 250)} - - [{stamp(t)}] "GET {path} HTTP/1.1" {status} {rng.randint(100, 9000)} rid={rid}'
        )
        app.append(json.dumps({"rid": rid, "user": user, "path": path, "ms": rng.randint(1, 900)}))
    write_lines(Path(out) / "nginx.log", nginx)
    write_lines(Path(out) / "app.jsonl", app)
    return {"params": {"path": target_path}, "expect": expect}
