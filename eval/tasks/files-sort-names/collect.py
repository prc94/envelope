import json
import os


def names(path):
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


print(
    json.dumps(
        {"inbox": names("inbox"), "spaced": names("sorted/spaced"), "plain": names("sorted/plain")}
    )
)
