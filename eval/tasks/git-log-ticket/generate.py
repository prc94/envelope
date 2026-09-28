from pathlib import Path

from _lib.gen import FIRST, LAST, VERBS, WORDS, make_repo, rng_for


def generate(out, seed):
    rng = rng_for(seed, "git")
    people = [
        (f"{f.capitalize()} {l.capitalize()}", f"{f}.{l}@example.com") for f, l in zip(FIRST, LAST)
    ]
    tickets = rng.sample(range(1000, 9999), 2500)
    target = rng.randint(800, 1700)
    commits, when = [], 1_700_000_000
    for i, t in enumerate(tickets):
        name, email = people[rng.randrange(len(people))]
        when += rng.randint(600, 40000)
        message = f"PROJ-{t}: {rng.choice(VERBS)} {rng.choice(WORDS)} {rng.choice(WORDS)}"
        commits.append(
            {
                "author": name,
                "email": email,
                "when": when,
                "message": message,
                "path": f"src/mod{i % 40}.txt",
                "content": f"revision {i}\n",
            }
        )
    make_repo(Path(out) / "repo", commits)
    return {"params": {"ticket": f"PROJ-{tickets[target]}"}, "expect": commits[target]["email"]}
