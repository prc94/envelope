import os
import subprocess
from pathlib import Path

from _lib.gen import rng_for


def generate(out, seed):
    rng = rng_for(seed, "conflict")
    a, b = rng.sample(range(10, 500), 2)
    repo = Path(out) / "repo"
    repo.mkdir(parents=True)
    when = 1_700_000_000 + seed % 1000
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.com",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.com",
        "GIT_AUTHOR_DATE": f"{when} +0000",
        "GIT_COMMITTER_DATE": f"{when} +0000",
    }

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    subprocess.run(["git", "init", "-q", "--template=", "-b", "main", str(repo)], check=True)
    for key, val in (
        ("user.name", "eval"),
        ("user.email", "eval@example.com"),
        ("core.autocrlf", "false"),
    ):
        git("config", key, val)
    base = "name=service\nretries=3\ntimeout=30\nverbose=false\n"
    (repo / "settings.txt").write_text(base)
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "feature")
    (repo / "settings.txt").write_text(base.replace("timeout=30", f"timeout={b}"))
    git("commit", "-q", "-am", "feature: change timeout")
    git("checkout", "-q", "main")
    (repo / "settings.txt").write_text(base.replace("timeout=30", f"timeout={a}"))
    git("commit", "-q", "-am", "main: change timeout")
    return {"params": {"strategy": "theirs" if b > a else "ours"}, "expect": str(max(a, b))}
