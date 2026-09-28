from pathlib import Path

from _lib.gen import WORDS, rng_for, token, write_lines

ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate(out, seed):
    rng = rng_for(seed, "grep")
    files = [f"src/pkg{i // 10}/mod{i}.py" for i in range(120)]
    bodies = {}
    for path in files:
        lines = []
        for _ in range(rng.randint(60, 150)):
            w = rng.choice(WORDS)
            lines.append(
                rng.choice(
                    [
                        f"    if attempts > max_retry_{w}: raise TooManyRetries()",
                        f"    self.{w}_retry_delay = base * 2",
                        f"def {w}_handler(request):",
                        f"    return {w}_cache.get(request.key)",
                        f"    # retry the {w} call with backoff",
                        f"    value = compute_{w}(request)",
                        f"    log.info('retry budget for {w} exhausted')",
                    ]
                )
            )
        bodies[path] = lines
    value = token(rng, 10, ALNUM)
    path = files[rng.randrange(len(files))]
    bodies[path].insert(rng.randrange(len(bodies[path])), f"    # retry note: nonce={value}")
    for path, lines in bodies.items():
        write_lines(Path(out) / path, lines)
    return {"params": {}, "expect": value}
