from pathlib import Path

from _lib.gen import rng_for, stamp, write_lines

CLASSES = [
    "KeyError",
    "ValueError",
    "TimeoutError",
    "ConnectionResetError",
    "PermissionError",
    "ZeroDivisionError",
    "AttributeError",
    "IndexError",
    "OSError",
    "RuntimeError",
    "UnicodeDecodeError",
    "FileNotFoundError",
]
NOISE = ["handled request", "refreshed session", "queued job", "flushed buffer", "cache miss"]


def generate(out, seed):
    rng = rng_for(seed, "tb")
    spots = set(rng.sample(range(500, 28000), 40))
    lines, t, last, previous = [], 1_700_000_000, None, None
    for i in range(30000):
        t += rng.randint(0, 2)
        if i in spots:
            previous, last = last, rng.choice([c for c in CLASSES if c != last])
            lines += [
                f"{stamp(t)} ERROR worker-{rng.randint(1, 8)} request failed",
                "Traceback (most recent call last):",
                '  File "/srv/app/handlers.py", line 88, in handle',
                "    result = process(item)",
                '  File "/srv/app/pipeline.py", line 142, in process',
                '    return transform(item["payload"])',
                f"{last}: unexpected state {rng.randint(1, 999)}",
            ]
        else:
            lines.append(f"{stamp(t)} INFO worker-{rng.randint(1, 8)} {rng.choice(NOISE)}")
    # a decoy after the last traceback: a class name that is not an exception line
    lines.append(f"{stamp(t + 1)} WARN worker-1 retrying after {previous} handled earlier")
    write_lines(Path(out) / "app.log", lines)
    return {"params": {}, "expect": last, "classes": CLASSES}
