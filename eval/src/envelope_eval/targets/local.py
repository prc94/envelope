"""Trial target on this host (POSIX): the same lifecycle as the Windows target without SSH.

Used to validate task oracles quickly and in tests; the Linux lane proper runs trials in
containers. The harness still runs through `launcher/launch.py`, in a session unit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..runner.trial import UnitResult, Workspace

LAUNCHER = Path(__file__).parents[3] / "launcher" / "launch.py"


class LocalTrialTarget:
    def __init__(self, root: Path, python: str = sys.executable) -> None:
        self.root, self.python = Path(root), python
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, trial_id: str) -> Workspace:
        base = self.root / trial_id
        shutil.rmtree(base, ignore_errors=True)
        state, spool, home = base / "state", base / "spool", base / "home"
        state.mkdir(parents=True)
        spool.mkdir()
        return Workspace(
            workspace=str(base / "workspace"),
            home=str(home),
            state=str(state),
            audit_log=str(state / "audit.jsonl"),
            spool=str(spool),
            env={
                "HOME": str(home),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
                "ENVELOPE_AUDIT_LOG": str(state / "audit.jsonl"),
                "ENVELOPE_SPOOL_DIR": str(spool),
            },
        )

    def real_path(self, remote: str) -> str:
        return remote

    def upload(self, local: Path, remote: str) -> None:
        if local.is_dir():
            shutil.copytree(local, remote)
        else:
            Path(remote).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, remote)

    def download(self, remote: str, local: Path) -> None:
        source = Path(remote)
        if not source.exists():
            raise FileNotFoundError(remote)
        if source.is_dir():
            shutil.copytree(source, local, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, local)

    def run_unit(
        self, argv: list[str], *, env: dict[str, str], cwd: str, timeout: float, stdout_to: str
    ) -> UnitResult:
        state = Path(stdout_to).parent
        spec = {
            "argv": argv,
            "cwd": cwd,
            "env": env,
            "timeout": timeout,
            "stdout": stdout_to,
            "result": str(state / "launch_result.json"),
        }
        (state / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(
            [self.python, str(LAUNCHER), str(state / "spec.json")],
            capture_output=True,
            text=True,
            timeout=timeout + 120,
            check=False,
        )
        if proc.returncode:
            raise RuntimeError(f"launcher failed ({proc.returncode}): {proc.stderr.strip()}")
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        if result.get("error"):
            raise RuntimeError(f"launcher could not start the harness: {result['error']}")
        return UnitResult(
            exit_code=result["exit_code"],
            timed_out=bool(result["timed_out"]),
            leftovers=tuple(f"{m['pid']} {m['name']}" for m in result["leftovers"]),
        )

    def collect(self, script: Path, ws: Workspace, timeout: float) -> tuple[int, bytes, bytes]:
        env = {**os.environ, **{k: v for k, v in ws.env.items() if k.startswith("ENVELOPE_")}}
        proc = subprocess.run(
            [self.python, "-X", "utf8", str(Path(script).resolve())],
            cwd=ws.workspace,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def cleanup(self, ws: Workspace) -> None:
        shutil.rmtree(Path(ws.state).parent, ignore_errors=True)

    def find_secrets(self, ws: Workspace, secrets: list[str]) -> list[str]:
        needles = [s.encode("utf-8") for s in secrets]
        hits = []
        for path in Path(ws.state).parent.rglob("*"):
            if path.is_file() and any(n in path.read_bytes() for n in needles):
                hits.append(str(path))
        return hits
