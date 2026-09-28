import subprocess
from pathlib import Path

from _lib.gen import FIRST, LAST, VERBS, WORDS, make_repo, rng_for, token


def generate(out, seed):
    rng = rng_for(seed, "hist")
    people = [
        (f"{f.capitalize()} {l.capitalize()}", f"{f}.{l}@example.com") for f, l in zip(FIRST, LAST)
    ]
    needle = f"FLAG_{token(rng, 8, 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789')}"
    added = rng.randint(400, 1100)
    removed = added + rng.randint(30, 200)
    commits, when = [], 1_700_000_000
    for i in range(1500):
        name, email = people[rng.randrange(len(people))]
        when += rng.randint(600, 40000)
        path, content = f"src/mod{i % 40}.txt", f"revision {i}\n"
        if i == added:
            path, content = "config/flags.txt", f"flag={needle}\n"
        elif i == removed:
            path, content = "config/flags.txt", "flag=none\n"
        message = f"{rng.choice(VERBS)} {rng.choice(WORDS)} {rng.choice(WORDS)}"
        commits.append(
            {
                "author": name,
                "email": email,
                "when": when,
                "message": message,
                "path": path,
                "content": content,
            }
        )
    repo = Path(out) / "repo"
    make_repo(repo, commits)
    found = subprocess.run(
        ["git", "-C", str(repo), "log", f"-S{needle}", "--format=%H", "--reverse"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {"params": {"needle": needle}, "expect": found[0][:7]}
