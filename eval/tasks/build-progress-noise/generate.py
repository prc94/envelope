import hashlib
from pathlib import Path

from _lib.gen import rng_for, token


def generate(out, seed):
    rng = rng_for(seed, "build")
    payload = bytes(rng.randrange(256) for _ in range(4096))
    salt = token(rng, 16)
    script = f"""#!/bin/bash
SALT={salt}
total=3000
echo "configuring"
for i in $(seq 1 $total); do
  printf '\\rbuilding [%4d/%d]' "$i" "$total"
done
printf '\\n'
echo "artifact-id: $(printf '%s' "$SALT$(sha256sum < payload.bin | cut -c1-64)" | sha256sum | cut -c1-16)"
"""
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "payload.bin").write_bytes(payload)
    (Path(out) / "build.sh").write_bytes(script.replace("\r\n", "\n").encode())
    digest = hashlib.sha256((salt + hashlib.sha256(payload).hexdigest()).encode()).hexdigest()
    return {"params": {}, "expect": digest[:16]}
