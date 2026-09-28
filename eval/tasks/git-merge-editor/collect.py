import json
import subprocess


def git(*args):
    proc = subprocess.run(["git", "-C", "repo", *args], capture_output=True, text=True, check=False)
    return proc.stdout.strip()


print(
    json.dumps(
        {
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "parents": len(git("rev-list", "--parents", "-n", "1", "HEAD").split()) - 1,
            "has_feature_file": bool(git("ls-files", "feature.txt")),
            "clean": git("status", "--porcelain") == "",
            "merge_in_progress": bool(git("rev-parse", "-q", "--verify", "MERGE_HEAD")),
        }
    )
)
