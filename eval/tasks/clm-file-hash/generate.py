import hashlib
from pathlib import Path

from _lib.gen import rng_for


def generate(out, seed):
    rng = rng_for(seed, "clm")
    data = bytes(rng.randrange(256) for _ in range(rng.randint(2000, 6000)))
    path = Path(out) / "payload.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"expect": hashlib.sha256(data).hexdigest()}
