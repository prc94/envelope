"""Observations from envelope's audit log (this is an envelope-mechanics task, so the judge may
read it). Runs on the target with the trial's ENVELOPE_AUDIT_LOG in its environment."""

import json
import os

rows = []
path = os.environ.get("ENVELOPE_AUDIT_LOG")
if path and os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
calls = [r for r in rows if r.get("event") == "tool_call"]
runs = [r for r in calls if r.get("tool") == "run"]
outputs = [r for r in calls if r.get("tool") == "output"]
print(
    json.dumps(
        {
            "runs": [
                {
                    "exit_code": r["outcome"].get("exit_code"),
                    "duration_ms": r["outcome"].get("duration_ms"),
                }
                for r in runs
            ],
            "outputs_ok": sum(1 for r in outputs if "error" not in r["outcome"]),
        }
    )
)
