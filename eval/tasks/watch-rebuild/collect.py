import hashlib
import json
import os
from pathlib import Path

path = Path(os.environ.get("ENVELOPE_AUDIT_LOG", ""))
rows = [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []
outputs = [row for row in rows if row.get("event") == "tool_call" and row.get("tool") == "output"]
source = Path("source.txt").read_bytes()
print(
    json.dumps(
        {
            "source": source.decode("utf-8", "replace"),
            "result": "RESULT " + hashlib.sha256(source).hexdigest()[:16],
            "reads": len(outputs),
        }
    )
)
