import hashlib
from pathlib import Path


def generate(out: str, seed: int):
    Path(out, "source.txt").write_text("version=1\n", encoding="utf-8")
    Path(out, "watch.py").write_text(
        """import hashlib
import time
from pathlib import Path

path = Path("source.txt")
previous = None
while True:
    current = path.read_bytes()
    if current != previous:
        print("RESULT " + hashlib.sha256(current).hexdigest()[:16], flush=True)
        previous = current
    time.sleep(0.1)
""",
        encoding="utf-8",
    )
    expected = "RESULT " + hashlib.sha256(b"version=2\n").hexdigest()[:16]
    return {"params": {"expect": expected}, "expect": expected}
