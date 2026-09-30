from pathlib import Path

from _lib.gen import rng_for

WORDS = ["yes", "confirm", "go", "proceed", "affirmative"]


def generate(out, seed):
    rng = rng_for(seed, "stdin")
    word = rng.choice(WORDS)
    script = (
        "try:\n"
        "    answer = input('Continue? [y/n]: ')\n"
        "except EOFError:\n"
        "    answer = ''\n"
        "print('ANSWER=' + answer.strip().lower())\n"
    )
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "confirm.py").write_text(script, encoding="utf-8")
    return {"expect": word, "params": {"confirm_word": word}}
