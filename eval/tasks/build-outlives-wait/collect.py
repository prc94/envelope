import json
import os
from pathlib import Path

path = Path(os.environ.get("ENVELOPE_AUDIT_LOG", ""))
rows = [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []
runs = [row for row in rows if row.get("event") == "tool_call" and row.get("tool") == "run"]
outputs = [row for row in rows if row.get("event") == "tool_call" and row.get("tool") == "output"]
print(
    json.dumps(
        {
            "handed_back": any(
                (row.get("outcome") or {}).get("state") == "running" for row in runs
            ),
            "continued": any(
                (row.get("outcome") or {}).get("wait", {}).get("outcome") == "condition_met"
                for row in outputs
            ),
        }
    )
)
