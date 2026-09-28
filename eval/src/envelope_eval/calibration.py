"""Console calibration (design E8): do native tools write the same bytes when started from an
SSH session (no console) as in the interactive session a real user works in?

The same launcher, spec and script run in both contexts, so the only difference between them is
the session: the SSH one is started by `sshd`, the interactive one by a scheduled task in the
logged-on user's session (`schtasks /it`)."""

from __future__ import annotations

import base64
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .targets.ssh import SSHTarget

CALIBRATE = Path(__file__).parents[2] / "launcher" / "calibrate.py"


@dataclass
class Comparison:
    identical: list[str] = field(default_factory=list)
    different: dict[str, dict[str, Any]] = field(default_factory=dict)
    info: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def same(self) -> bool:
        return not self.different


def decode(entry: dict[str, Any], stream: str) -> bytes:
    return base64.b64decode(entry.get(stream, ""))


def compare(ssh: dict[str, Any], interactive: dict[str, Any]) -> Comparison:
    """Byte-for-byte comparison of two captures, command by command."""
    out = Comparison(info={"ssh": ssh["info"], "interactive": interactive["info"]})
    a = {c["name"]: c for c in ssh["commands"]}
    b = {c["name"]: c for c in interactive["commands"]}
    for name, left in a.items():
        right = b.get(name, {"error": "missing from the interactive capture"})
        if "error" in left or "error" in right:
            if left.get("error") != right.get("error"):
                out.different[name] = {"ssh": left.get("error"), "interactive": right.get("error")}
            else:
                out.identical.append(name)
            continue
        same = (
            left["returncode"] == right["returncode"]
            and decode(left, "stdout") == decode(right, "stdout")
            and decode(left, "stderr") == decode(right, "stderr")
        )
        if same:
            out.identical.append(name)
        else:
            out.different[name] = {
                "ssh": {
                    "rc": left["returncode"],
                    "stdout": decode(left, "stdout").hex(),
                    "stderr": decode(left, "stderr").hex(),
                },
                "interactive": {
                    "rc": right["returncode"],
                    "stdout": decode(right, "stdout").hex(),
                    "stderr": decode(right, "stderr").hex(),
                },
            }
    return out


def run_calibration(
    ssh: SSHTarget, root: str = "C:\\eval", timeout: float = 300
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the capture from SSH and from the interactive session; returns both captures."""
    work = f"{root}\\calibration"
    ssh.run_shell(f'if exist "{work}" rmdir /s /q "{work}"')
    with tempfile.TemporaryDirectory() as tmp:
        docs = Path(tmp) / "docs"
        docs.mkdir()
        for name in ("отчёт-март-7.txt", "договор.txt", "счёт 12.txt", "plan.txt"):
            (docs / name).write_text("x", encoding="utf-8")
        ssh.run_shell(f'mkdir "{work}"')
        ssh.put(docs, f"{work}\\docs")
        ssh.put(CALIBRATE, f"{work}\\calibrate.py")
        captures = {}
        for context in ("ssh", "interactive"):
            out = f"{work}\\{context}.json"
            spec = {
                "argv": [ssh.python, f"{work}\\calibrate.py", out, work],
                "cwd": work,
                "timeout": 240,
                "stdout": f"{work}\\{context}.stdout",
                "result": f"{work}\\{context}.launch.json",
            }
            spec_file = Path(tmp) / f"{context}.spec.json"
            spec_file.write_text(json.dumps(spec), encoding="utf-8")
            ssh.put(spec_file, f"{work}\\{context}.spec.json")
            launch = f"{ssh.python} {root}\\launcher\\launch.py {work}\\{context}.spec.json"
            if context == "ssh":
                rc, _, err = ssh.run_shell(launch, timeout=timeout)
                if rc:
                    raise RuntimeError(
                        f"ssh capture failed ({rc}): {err.decode('utf-8', 'replace')}"
                    )
            else:
                _run_interactive(ssh, launch, out, timeout)
            local = Path(tmp) / f"{context}.json"
            ssh.get(out, local)
            captures[context] = json.loads(local.read_text(encoding="utf-8"))
    ssh.run_shell(f'rmdir /s /q "{work}"')
    return captures["ssh"], captures["interactive"]


def _run_interactive(ssh: SSHTarget, command: str, out: str, timeout: float) -> None:
    """Start `command` in the logged-on user's interactive session with a scheduled task."""
    name = "envelope-eval-calibration"
    ssh.run_shell(f"schtasks /delete /tn {name} /f")
    rc, _, err = ssh.run_shell(
        f'schtasks /create /tn {name} /tr "{command}" /sc once /st 23:59 /it /f'
    )
    if rc:
        raise RuntimeError(f"schtasks /create failed: {err.decode('utf-8', 'replace')}")
    rc, _, err = ssh.run_shell(f"schtasks /run /tn {name}")
    if rc:
        raise RuntimeError(f"schtasks /run failed: {err.decode('utf-8', 'replace')}")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            rc, _, _ = ssh.run_shell(f'if exist "{out}" (exit 0) else (exit 1)')
            if rc == 0:
                time.sleep(1)
                return
            time.sleep(3)
        raise TimeoutError("the interactive capture did not finish (is a user logged on?)")
    finally:
        ssh.run_shell(f"schtasks /delete /tn {name} /f")


def render(result: Comparison) -> str:
    lines = ["# Console calibration", ""]
    for context, info in result.info.items():
        lines.append(
            f"- {context}: session {info['session_id']} ({info['sessionname_env']}), user "
            f"{info['username']}, ACP {info['acp']}, OEMCP {info['oemcp']}, console CP in/out "
            f"{info['console_input_cp']}/{info['console_output_cp']}, console window: {info['has_console_window']}"
        )
    lines += [
        "",
        f"{len(result.identical)} of {len(result.identical) + len(result.different)} commands wrote identical bytes.",
    ]
    for name, diff in result.different.items():
        lines.append(f"- DIFFERENT {name}: ssh {diff['ssh']} | interactive {diff['interactive']}")
    return "\n".join(lines) + "\n"
