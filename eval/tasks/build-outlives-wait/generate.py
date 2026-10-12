from _lib.background_jobs import delayed_job


def generate(out: str, seed: int):
    return delayed_job(out, seed, "build-outlives-wait", 70)
