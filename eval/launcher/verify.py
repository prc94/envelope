#!/usr/bin/env python3
"""Self-check for launch.py with stand-in harnesses. Standard library only; runs on any host,
including the Windows VM (`python verify.py`), and is also used by the test suite.

Checks: a harness that leaves a background child (reported as a leftover, then killed), one that
exceeds its timeout (the whole tree, grandchildren included, is gone), a clean exit with the
exit code and the transcript bytes preserved, and that a process outside the unit (standing in
for the SSH tunnel) is untouched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
TRANSCRIPT = "привет ✓ тест\n".encode()

STANDIN = """
import os, subprocess, sys, time

role, pidfile, depth = sys.argv[1], sys.argv[2], int(sys.argv[3])
flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
if role == "ok":
    sys.stdout.buffer.write(__TEXT__)
    sys.stdout.buffer.flush()
    raise SystemExit(0)
if role == "exit7":
    raise SystemExit(7)
if role == "sleep":
    time.sleep(600)
child = subprocess.Popen(
    [sys.executable, __file__, "child" if depth > 0 else "sleep", pidfile, str(depth - 1)],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=flags,
)
with open(pidfile, "a") as fh:
    fh.write(str(child.pid) + "\\n")
if role == "hang" or role == "child":
    time.sleep(600)
""".replace("__TEXT__", repr(TRANSCRIPT))


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        sys.path.insert(0, str(HERE))
        import launch  # type: ignore[import-not-found]

        return pid in launch.process_names()
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            stat = fh.read()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def read_pids(path: Path, want: int, wait: float = 10) -> list[int]:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        pids = [int(p) for p in path.read_text().split()] if path.exists() else []
        if len(pids) >= want:
            return pids
        time.sleep(0.1)
    return [int(p) for p in path.read_text().split()] if path.exists() else []


def launch(tmp: Path, name: str, role: str, depth: int, timeout: float) -> dict[str, Any]:
    standin = tmp / "standin.py"
    standin.write_text(STANDIN, encoding="utf-8")
    spec = {
        "argv": [sys.executable, str(standin), role, str(tmp / f"{name}.pids"), str(depth)],
        "cwd": str(tmp),
        "timeout": timeout,
        "stdout": str(tmp / f"{name}.out"),
        "result": str(tmp / f"{name}.result.json"),
        "grace": 10,
    }
    (tmp / f"{name}.spec.json").write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(HERE / "launch.py"), str(tmp / f"{name}.spec.json")],
        capture_output=True,
        text=True,
        timeout=timeout + 60,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"launcher failed: {proc.stderr[-500:]}")
    return json.loads(Path(spec["result"]).read_text(encoding="utf-8"))


def check_clean_exit(tmp: Path) -> None:
    result = launch(tmp, "ok", "ok", 0, 30)
    assert result["exit_code"] == 0 and not result["timed_out"], result
    assert result["leftovers"] == [] and result["emptied"] and result["error"] is None, result
    assert (tmp / "ok.out").read_bytes() == TRANSCRIPT, "transcript bytes changed"
    assert launch(tmp, "seven", "exit7", 0, 30)["exit_code"] == 7


def check_leftover(tmp: Path) -> None:
    result = launch(tmp, "leave", "leave", 0, 30)  # depth 0: the child is a plain sleeper
    (child,) = read_pids(tmp / "leave.pids", 1)
    assert result["exit_code"] == 0 and not result["timed_out"], result
    assert child in [m["pid"] for m in result["leftovers"]], result
    assert result["emptied"] and not alive(child), "leftover was not killed"


def check_timeout(tmp: Path) -> None:
    started = time.monotonic()
    result = launch(tmp, "hang", "hang", 1, 5)  # child and grandchild
    pids = read_pids(tmp / "hang.pids", 2)
    assert len(pids) == 2, pids
    assert result["timed_out"] and result["exit_code"] is None, result
    seen = {m["pid"] for m in result["at_timeout"]}
    assert set(pids) <= seen and len(seen) >= 3, (pids, result["at_timeout"])
    assert result["emptied"] and not any(alive(p) for p in pids), "tree survived the timeout"
    assert time.monotonic() - started < 60


def main() -> int:
    outside = subprocess.Popen(  # stands in for the SSH tunnel: never inside the unit
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as raw:
            for check in (check_clean_exit, check_leftover, check_timeout):
                sub = Path(raw) / check.__name__
                sub.mkdir()
                try:
                    check(sub)
                    print(f"ok   {check.__name__}")
                except (AssertionError, RuntimeError) as exc:
                    failures.append(check.__name__)
                    print(f"FAIL {check.__name__}: {exc}")
        if outside.poll() is not None or not alive(outside.pid):
            failures.append("outside process was killed")
            print("FAIL a process outside the unit was killed")
        else:
            print("ok   process outside the unit untouched")
    finally:
        outside.kill()
        outside.wait()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
