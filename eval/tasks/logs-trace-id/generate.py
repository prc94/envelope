from pathlib import Path

from _lib.gen import rng_for, stamp, token, write_lines

SERVICES = ["gateway", "auth", "orders", "billing", "search", "notify"]
NOISE = ["cache miss", "handled request", "refreshed session", "queued job", "flushed buffer"]


def generate(out, seed):
    rng = rng_for(seed, "trace")
    target, code = token(rng, 12), f"E{rng.randint(100, 999)}"
    failing = rng.choice(SERVICES[1:])
    lines = {}
    for service in SERVICES:
        t, rows = 1_700_000_000 + rng.randint(0, 1000), []
        for _ in range(3500):
            t += rng.randint(0, 3)
            level = rng.choices(["INFO", "DEBUG", "WARN", "ERROR"], [70, 20, 7, 3])[0]
            message = rng.choice(NOISE)
            if level == "ERROR":
                message = f"upstream call failed code=E{rng.randint(100, 999)}"
            rows.append([t, level, token(rng, 12), message])
        for _ in range(45):  # the target trace is chatty in every service
            row = rows[rng.randrange(len(rows))]
            row[1], row[2], row[3] = rng.choice(["INFO", "DEBUG"]), target, rng.choice(NOISE)
        lines[service] = rows
    row = lines[failing][rng.randrange(len(lines[failing]))]
    row[1], row[2], row[3] = "ERROR", target, f"payment declined code={code}"
    for service, rows in lines.items():
        write_lines(
            Path(out) / "logs" / f"{service}.log",
            (f"{stamp(t)} {lv} {service} [trace={tr}] {msg}" for t, lv, tr, msg in rows),
        )
    return {"params": {"trace": target}, "expect": code}
