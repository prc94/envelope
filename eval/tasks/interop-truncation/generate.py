import random

PRIMES = [7, 11, 13, 17, 19, 23, 29, 31, 37, 41]


def generate(out: str, seed: int):
    rng = random.Random(seed)
    factors = rng.sample(PRIMES, 4)
    product = 1
    for f in factors:
        product *= f
    line = rng.randint(9000, 19000)
    params = {"line": line, "factors": "*".join(str(f) for f in factors)}
    return {"params": params, "expect": str(product), "secrets": [f"TOKEN={product}"]}
