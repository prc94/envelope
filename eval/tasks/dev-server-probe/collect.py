import json
from pathlib import Path

path = Path("hits.log")
hits = path.read_text().splitlines() if path.exists() else []
print(json.dumps({"hits": len(hits), "value": hits[-1].split()[-1] if hits else None}))
