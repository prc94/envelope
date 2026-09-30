from pathlib import Path


def generate(out, seed):
    # No fixture bytes: the copied executable comes from the VM's own System32, which the host
    # cannot supply. The generator only needs to exist (the loader requires every task part).
    Path(out).mkdir(parents=True, exist_ok=True)
    return {"expect": "126"}
