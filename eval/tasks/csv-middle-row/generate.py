from pathlib import Path

from _lib.gen import rng_for, stamp, write_lines


def generate(out, seed):
    rng = rng_for(seed, "csv")
    target = rng.randint(20000, 40000)
    rows, expect = ["id,ts,host,value"], None
    for i in range(1, 60001):
        value = f"{rng.uniform(0, 1000):.3f}"
        if i == target:
            expect = value
        rows.append(
            f"r{i:06d},{stamp(1_700_000_000 + i * 15)},host-{rng.randint(1, 40):02d},{value}"
        )
    write_lines(Path(out) / "metrics.csv", rows)
    return {"params": {"id": f"r{target:06d}"}, "expect": expect}
