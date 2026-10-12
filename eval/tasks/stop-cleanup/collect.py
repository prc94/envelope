import json
import os
from pathlib import Path

path = Path(os.environ.get("ENVELOPE_AUDIT_LOG", ""))
rows = [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []
runs = [row for row in rows if row.get("event") == "tool_call" and row.get("tool") == "run"]
stops = [row for row in rows if row.get("event") == "tool_call" and row.get("tool") == "stop"]
ends = [row for row in rows if row.get("event") == "process_end"]
print(
    json.dumps(
        {
            "handed_back": any(
                (row.get("outcome") or {}).get("state") == "running" for row in runs
            ),
            "cleaned": bool(stops) or any(row.get("end_reason") != "shutdown" for row in ends),
        }
    )
)
