import glob
import json
import os

files = {}
for path in glob.glob("legacy/*.txt"):
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        files[os.path.basename(path)] = {"utf8": True, "text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        files[os.path.basename(path)] = {"utf8": False, "text": ""}
print(json.dumps({"files": files}))
