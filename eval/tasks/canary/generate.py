from pathlib import Path


def generate(out: str, seed: int):
    Path(out).mkdir(parents=True, exist_ok=True)
    return {"answer": "canary", "seed": seed}
