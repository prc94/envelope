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
MONTHS = ["январь", "март", "июнь", "октябрь", "May", "June", "Q3", "2024"]


def generate(out, seed):
    rng = rng_for(seed, "names")
    inbox = Path(out) / "inbox"
    inbox.mkdir(parents=True)
    names = set()
    while len(names) < 30:
        parts = [rng.choice(WORDS), rng.choice(MONTHS)]
        sep = rng.choice([" ", " ", "-", "_"])
        names.add(sep.join(parts) + f"{sep}{rng.randint(1, 99)}.txt")
    for name in names:
        (inbox / name).write_text("документ\n", encoding="utf-8")
    return {
        "params": {},
        "expect": {
            "spaced": sorted(n for n in names if " " in n),
            "plain": sorted(n for n in names if " " not in n),
        },
    }
