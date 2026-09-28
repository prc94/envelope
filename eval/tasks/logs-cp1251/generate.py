from pathlib import Path

from _lib.gen import rng_for, stamp, token, write_lines

PHRASES = [
    "оплата отклонена",
    "превышен лимит запросов",
    "сессия истекла",
    "счёт не найден",
    "ошибка подключения",
    "недостаточно средств",
    "доступ запрещён",
    "повторная попытка",
]
NOISE = ["запрос обработан", "кэш обновлён", "задание поставлено в очередь", "буфер сброшен"]


def generate(out, seed):
    rng = rng_for(seed, "cp1251")
    target = rng.choice(PHRASES)
    t, rows = 1_700_000_000, []
    for _ in range(9000):
        t += rng.randint(0, 3)
        phrase = rng.choice([p for p in PHRASES if p != target] + NOISE)
        rows.append(f"{stamp(t)} INFO req={token(rng, 10)} {phrase}")
    at = rng.randrange(len(rows))
    expect = token(rng, 10)
    t_at = rows[at][:19]
    rows[at] = f"{t_at} ERROR req={expect} {target}"
    write_lines(Path(out) / "service-ru.log", rows, eol="\r\n", encoding="cp1251")
    return {"params": {"phrase": target}, "expect": expect}
