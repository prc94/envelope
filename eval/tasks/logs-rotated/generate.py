import gzip
from pathlib import Path

from _lib.gen import rng_for, stamp


def generate(out, seed):
    rng = rng_for(seed, "rot")
    logs = Path(out) / "logs"
    logs.mkdir(parents=True)
    t, total = 1_700_000_000, 0
    for name, gz in (
        ("svc.log.3.gz", True),
        ("svc.log.2.gz", True),
        ("svc.log.1", False),
        ("svc.log", False),
    ):
        lines = []
        for _ in range(rng.randint(3000, 5000)):
            t += rng.randint(0, 2)
            level = rng.choices(["INFO", "DEBUG", "WARN", "ERROR"], [70, 18, 6, 6])[0]
            total += level == "ERROR"
            lines.append(f"{stamp(t)} {level} svc handled request {rng.randint(1, 99999)}")
        data = ("\n".join(lines) + "\n").encode()
        if gz:
            # mtime=0 keeps the archive bytes reproducible from the seed
            (logs / name).write_bytes(gzip.compress(data, mtime=0))
        else:
            (logs / name).write_bytes(data)
    return {"params": {}, "expect": total}
