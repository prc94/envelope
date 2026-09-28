import hashlib
from pathlib import Path

from _lib.gen import rng_for

WORDS = [
    "отчёт",
    "договор",
    "накладная",
    "счёт",
    "приказ",
    "протокол",
    "смета",
    "акт",
    "заявка",
    "справка",
    "письмо",
    "график",
    "реестр",
    "баланс",
    "анализ",
    "план",
    "ведомость",
    "заказ",
]


def generate(out, seed):
    rng = rng_for(seed, "dups")
    data = Path(out) / "data"
    data.mkdir(parents=True)
    contents = [
        f"содержимое {i} {rng.randint(1000, 9999)}\n" * rng.randint(1, 5) for i in range(22)
    ]
    files = {}
    while len(files) < 45:
        name = f"{rng.choice(WORDS)}{rng.choice([' ', '_', '-'])}{rng.randint(1, 999)}.txt"
        if name not in files:
            files[name] = rng.choice(contents) if len(files) >= 22 else contents[len(files)]
    for name, text in files.items():
        (data / name).write_bytes(text.encode("utf-8"))
    keep = {}
    for name in sorted(files):  # code-point order
        keep.setdefault(hashlib.sha256(files[name].encode()).hexdigest(), name)
    return {"params": {}, "expect": sorted(keep.values())}
