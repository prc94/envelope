from pathlib import Path


def generate(out: str, seed: int):
    Path(out, "server.py").write_text(
        "import time\nprint('READY', flush=True)\nwhile True: time.sleep(1)\n",
        encoding="utf-8",
    )
    return {"expect": "done"}
