from pathlib import Path

from _lib.gen import rng_for, write_lines

STATUSES = ["ready", "pending", "failed", "ready-check", "not-ready", "retry", "Ready", "ready "]


def generate(out, seed):
    rng = rng_for(seed, "crlf")
    rows = [rng.choice(STATUSES) for _ in range(6000)]
    write_lines(Path(out) / "status.txt", rows, eol="\r\n")
    return {"params": {}, "expect": sum(1 for r in rows if r == "ready")}
