from pathlib import Path

from _lib.gen import rng_for, stamp, write_lines

NOISE = ["handled request", "refreshed session", "queued job", "flushed buffer", "cache miss"]


def generate(out, seed):
    rng = rng_for(seed, "win")
    start = 1_700_006_400  # 2023-11-15 00:00:00 UTC + 6400 s: the day begins in the log
    t, rows = start, []
    for _ in range(40000):
        t += rng.choice([0, 0, 1, 1, 2])
        level = rng.choices(["INFO", "DEBUG", "WARN", "ERROR"], [70, 18, 6, 6])[0]
        rows.append((t, level, rng.choice(NOISE)))
    write_lines(Path(out) / "service.log", (f"{stamp(a)} {b} {c}" for a, b, c in rows))
    lo = start + rng.randint(3000, 7000)
    hi = lo + rng.randint(1200, 2400)
    t1, t2 = stamp(lo)[11:], stamp(hi)[11:]
    count = sum(1 for a, b, _ in rows if b == "ERROR" and t1 <= stamp(a)[11:] <= t2)
    return {"params": {"t1": t1, "t2": t2}, "expect": count}
