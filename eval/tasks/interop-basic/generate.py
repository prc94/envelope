import random


def generate(out: str, seed: int):
    rng = random.Random(seed)
    a, b = rng.randint(11, 97), rng.randint(11, 97)
    expect = f"interop-{a * b}"
    return {"params": {"a": a, "b": b}, "expect": expect, "secrets": [expect]}
