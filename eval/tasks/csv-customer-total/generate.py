import csv
import io
from pathlib import Path

from _lib.gen import rng_for

CUSTOMERS = [
    "Иванов, Пётр",
    "Петрова Анна",
    "ООО Ромашка",
    "Смирнов, А. В.",
    "Козлов Игорь",
    "ИП Сидоров",
    "Николаева, Мария",
    "Орлов Денис",
]
NOTES = ["срочно", "оплата, частично", 'доставка "утро"', "", "повторный заказ", "без комментариев"]


def generate(out, seed):
    rng = rng_for(seed, "csv2")
    target = rng.choice(CUSTOMERS)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["order_id", "customer", "note", "amount"])
    total = 0
    for i in range(1, 3001):
        customer = rng.choice(CUSTOMERS)
        cents = rng.randint(500, 99999)
        if customer == target:
            total += cents
        writer.writerow([f"A{i:05d}", customer, rng.choice(NOTES), f"{cents / 100:.2f}"])
    (Path(out)).mkdir(parents=True, exist_ok=True)
    (Path(out) / "orders.csv").write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))
    return {"params": {"customer": target}, "expect": f"{total / 100:.2f}"}
