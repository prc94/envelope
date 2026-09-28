from pathlib import Path

from _lib.gen import rng_for, stamp, write_lines

NOISE = ["handled request", "refreshed session", "queued job", "flushed buffer", "cache miss"]


def generate(out, seed):
    rng = rng_for(seed, "u16")
    t, rows, count = 1_700_000_000, [], 0
    for _ in range(12000):
        t += rng.randint(0, 3)
        if rng.random() < 0.012:
            rows.append(f"{stamp(t)} WARN gateway upstream TIMEOUT after {rng.randint(5, 60)}s")
            count += 1
        else:
            rows.append(f"{stamp(t)} INFO worker-{rng.randint(1, 8)} {rng.choice(NOISE)}")
    write_lines(Path(out) / "app16.log", rows, eol="\r\n", encoding="utf-16-le", bom=b"\xff\xfe")
    return {"params": {}, "expect": count}
