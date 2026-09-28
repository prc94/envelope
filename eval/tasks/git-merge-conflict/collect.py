import json
import subprocess
from pathlib import Path


def git(*args):
    proc = subprocess.run(["git", "-C", "repo", *args], capture_output=True, text=True, check=False)
    return proc.stdout.strip()


text = Path("repo/settings.txt").read_text(encoding="utf-8")
values = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("timeout=")]
print(
    json.dumps(
        {
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "parents": len(git("rev-list", "--parents", "-n", "1", "HEAD").split()) - 1,
            "merge_in_progress": bool(git("rev-parse", "-q", "--verify", "MERGE_HEAD")),
            "conflict_markers": "<<<<<<<" in text or ">>>>>>>" in text,
            "timeout": values[0] if len(values) == 1 else None,
        }
    )
)
