from pathlib import Path

from _lib.gen import WORDS, rng_for, write_lines


def generate(out, seed):
    rng = rng_for(seed, "blog")
    modules = [f"mod_{w}{i}" for i, w in enumerate(WORDS * 30)]
    rng.shuffle(modules)
    first_at = rng.randint(60, 400)
    lines, expect = [], None
    for i in range(25000):
        module = modules[i % len(modules)]
        lines.append(f"[{i * 100 // 25000:3d}%] Compiling {module}")
        if i == first_at:
            expect = module
            lines.append(
                f"src/{module}.c:41: error: undefined reference to `init_{rng.choice(WORDS)}'"
            )
        elif i > first_at and i % 37 == 0:
            lines.append(
                f"src/{module}.c:{rng.randint(5, 900)}: error: cascade from earlier failure"
            )
        elif i % 11 == 0:
            lines.append(f"src/{module}.c:{rng.randint(5, 900)}: warning: unused variable")
    lines.append("make: *** [all] Error 2")
    write_lines(Path(out) / "build.log", lines)
    return {"params": {}, "expect": expect}
