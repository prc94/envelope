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
    rng = rng_for(seed, "conv")
    legacy = Path(out) / "legacy"
    legacy.mkdir(parents=True)
    expect = {}
    for i in range(12):
        lines = [
            " ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 8)))
            for _ in range(rng.randint(5, 40))
        ]
        text = "\r\n".join(lines) + "\r\n"
        name = f"{rng.choice(WORDS)}{i}.txt"
        (legacy / name).write_bytes(text.encode("cp1251"))
        expect[name] = text
    return {"params": {}, "expect": expect}
