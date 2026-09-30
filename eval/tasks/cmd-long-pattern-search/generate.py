from pathlib import Path

from _lib.gen import rng_for, token, write_lines


def generate(out, seed):
    rng = rng_for(seed, "cmdlen")
    needle = token(rng, 8500, alphabet="abcdefghijklmnopqrstuvwxyz0123456789")
    n_lines = rng.randint(200, 500)
    target_line = rng.randint(1, n_lines)

    lines = []
    for i in range(1, n_lines + 1):
        lines.append(needle if i == target_line else token(rng, rng.randint(10, 60)))

    write_lines(Path(out) / "haystack.txt", lines)
    write_lines(Path(out) / "needle.txt", [needle])
    return {"expect": str(target_line)}
