import os
import subprocess
from pathlib import Path


def generate(out, seed):
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
    git("config", "user.name", "eval")
    git("config", "user.email", "eval@example.com")
    git("config", "core.autocrlf", "false")
    (repo / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text(f"feature work {seed}\n")
    git("add", ".")
    git("commit", "-q", "-m", "feature work")
    git("checkout", "-q", "main")
    (repo / "main.txt").write_text(f"main work {seed}\n")
    git("add", ".")
    git("commit", "-q", "-m", "main work")
    return {"params": {}, "expect": "merge"}
