from pathlib import Path

from _lib.gen import WORDS, rng_for, token


def generate(out, seed):
    rng = rng_for(seed, "longpath")
    archive = Path(out) / "archive"
    target = f"note-{token(rng, 8)}.txt"
    expect = f"marker-{token(rng, 12)}"
    # Seven levels of 45-character names put the deepest files far past Windows' 260-character
    # limit once the trial directory is added in front.
    levels = [f"{rng.choice(WORDS)}-{token(rng, 8)}-{'x' * 28}" for _ in range(7)]
    deep = archive.joinpath(*levels)
    deep.mkdir(parents=True)
    for depth in range(1, 8):
        folder = archive.joinpath(*levels[:depth])
        for i in range(3):
            (folder / f"file-{depth}-{i}.txt").write_text(f"filler {rng.random()}\n")
    holder = archive.joinpath(*levels[: rng.randint(5, 7)])
    (holder / target).write_text(expect + "\nsecond line\n")
    return {"params": {"name": target}, "expect": expect, "path_length": len(str(holder / target))}
