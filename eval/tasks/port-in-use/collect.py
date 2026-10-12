import json
import os
from pathlib import Path

path = Path(os.environ.get("ENVELOPE_AUDIT_LOG", ""))
rows = [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []
runs = [row for row in rows if row.get("event") == "tool_call" and row.get("tool") == "run"]
hits = Path("hits.log").read_text().splitlines() if Path("hits.log").exists() else []
print(
    json.dumps(
        {
            "identity": hits[-1] if hits else None,
            "port_hint": any(
                "port_in_use" in (row.get("outcome") or {}).get("hints", []) for row in runs
            ),
        }
    )
)
