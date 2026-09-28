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
]
TYPES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август"]


def generate(out, seed):
    rng = rng_for(seed, "cp866")
    docs = Path(out) / "docs"
    docs.mkdir(parents=True)
    names = [f"{w}-{t}-{n}.txt" for w in WORDS for t in TYPES for n in (1,)]
    rng.shuffle(names)
    names = names[:40]
    target = rng.choice(WORDS)
    for extra in list(names):
        if target in extra:
            names.remove(extra)
    expect = f"{target}-{rng.choice(TYPES)}-7.txt"
    names.append(expect)
    for name in names:
        (docs / name).write_text("документ\n", encoding="utf-8")
    return {"params": {"word": target}, "expect": expect}
