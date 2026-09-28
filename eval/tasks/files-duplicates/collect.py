import json
import os

print(json.dumps({"files": sorted(os.listdir("data")) if os.path.isdir("data") else []}))
