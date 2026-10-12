"""Windows trial target over SSH: per-trial directories, the containment launcher, collect."""

from __future__ import annotations

import json
import ntpath
import tempfile
from pathlib import Path

from ..runner.trial import UnitResult, Workspace
from .ssh import SSHTarget

LAUNCHER_DIR = Path(__file__).parents[3] / "launcher"

# Runs on the target (streamed to `python -`); needles come in through the script text so they
# never appear on a command line.
FIND_SECRETS = """
import json, os, sys
needles = [n.encode("utf-8") for n in json.loads(%(needles)r)]
hits = []
for base, _dirs, files in os.walk(%(root)r):
    for name in files:
        path = os.path.join(base, name)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if any(n in data for n in needles):
            hits.append(path)
print(json.dumps(hits))
"""


def q(path: str) -> str:
    return f'"{path}"'


class WindowsTrialTarget:
    """`UnitTarget` for a Windows host: every trial gets its own directories under
    `<root>\\trials\\<trial>` (outside %TEMP%, whose script rules the VM enforces), its own HOME
    and app-data directories, its own git global config, and its own envelope audit log and
    spool, so concurrent trials share none of them."""

    def __init__(self, ssh: SSHTarget, root: str = "C:\\eval", python: str = "python") -> None:
        self.ssh, self.root, self.python = ssh, root, python
        self.launcher = f"{root}\\launcher"

    def _sh(self, command: str, timeout: float = 120) -> tuple[int, bytes, bytes]:
        return self.ssh.run_shell(command, timeout=timeout)

    def _must(self, command: str, what: str) -> None:
        rc, _, err = self._sh(command)
        if rc:
            raise RuntimeError(f"{what} failed ({rc}): {err.decode('utf-8', 'replace').strip()}")

    def mkdir(self, *paths: str) -> None:
        self._must(" & ".join(f"if not exist {q(p)} mkdir {q(p)}" for p in paths), "mkdir")

    # -- once per job ----------------------------------------------------------------------

    def setup(self, envelope_pyz: Path | None = None) -> None:
        """Create the eval directories as the standard user and install the launcher files."""
        self.mkdir(self.root, f"{self.root}\\trials", self.launcher)
        for name in ("launch.py", "oracle.py"):
            self.ssh.put(LAUNCHER_DIR / name, f"{self.launcher}\\{name}")
        if envelope_pyz is not None:
            self.ssh.put(envelope_pyz, f"{self.root}\\envelope.pyz")

    # -- UnitTarget ------------------------------------------------------------------------

    def prepare(self, trial_id: str) -> Workspace:
        base = f"{self.root}\\trials\\{trial_id}"
        home = f"{base}\\home"
        state, spool = f"{base}\\state", f"{base}\\spool"
        # A leftover directory from an earlier run of the same job would leak state into this one.
        self._must(f"if exist {q(base)} rmdir /s /q {q(base)}", "clear trial directory")
        self.mkdir(state, spool)
        # `workspace` and `home` are created by the uploads (a tree must not exist yet).
        return Workspace(
            workspace=f"{base}\\workspace",
            home=home,
            state=state,
            audit_log=f"{state}\\audit.jsonl",
            spool=spool,
            env={
                "HOME": home,
                "USERPROFILE": home,
                "APPDATA": f"{home}\\AppData\\Roaming",
                "LOCALAPPDATA": f"{home}\\AppData\\Local",
                "GIT_CONFIG_GLOBAL": f"{home}\\.gitconfig",
                "ENVELOPE_AUDIT_LOG": f"{state}\\audit.jsonl",
                "ENVELOPE_SPOOL_DIR": spool,
            },
        )

    def real_path(self, remote: str) -> str:
        return remote

    def upload(self, local: Path, remote: str) -> None:
        self.ssh.put(local, remote)

    def download(self, remote: str, local: Path) -> None:
        self.ssh.get(remote, local)

    def run_unit(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: str,
        timeout: float,
        stdout_to: str,
        stdin_from: str | None = None,
    ) -> UnitResult:
        state = ntpath.dirname(stdout_to)
        self.mkdir(env["APPDATA"], env["LOCALAPPDATA"])  # programs expect these to exist
        result_path = f"{state}\\launch_result.json"
        spec = {
            "argv": argv,
            "cwd": cwd,
            "env": env,
            "timeout": timeout,
            "stdout": stdout_to,
            "result": result_path,
        }
        if stdin_from is not None:
            spec["stdin"] = stdin_from
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "spec.json"
            local.write_text(json.dumps(spec), encoding="utf-8")
            self.ssh.put(local, f"{state}\\spec.json")
        rc, out, err = self._sh(
            f"{q(self.python)} {q(self.launcher + chr(92) + 'launch.py')} {q(state + chr(92) + 'spec.json')}",
            timeout=timeout + 300,
        )
        if rc:
            raise RuntimeError(f"launcher failed ({rc}): {err.decode('utf-8', 'replace').strip()}")
        result = json.loads(out.decode("utf-8").strip().splitlines()[-1])
        if result.get("error"):
            raise RuntimeError(f"launcher could not start the harness: {result['error']}")
        return UnitResult(
            exit_code=result["exit_code"],
            timed_out=bool(result["timed_out"]),
            leftovers=tuple(f"{m['pid']} {m['name']}" for m in result["leftovers"]),
        )

    def collect(self, script: Path, ws: Workspace, timeout: float) -> tuple[int, bytes, bytes]:
        remote = f"{ws.state}\\collect.py"
        self.ssh.put(script, remote)
        # Mechanics tasks read envelope's audit log, so collect sees the trial's ENVELOPE_* paths.
        env = "".join(f'set "{k}={v}"&& ' for k, v in ws.env.items() if k.startswith("ENVELOPE_"))
        return self._sh(
            f"{env}cd /d {q(ws.workspace)} && {q(self.python)} -X utf8 {q(remote)}", timeout=timeout
        )

    def cleanup(self, ws: Workspace) -> None:
        base = ntpath.dirname(ws.state)
        self._must(f"if exist {q(base)} rmdir /s /q {q(base)}", "cleanup")

    # -- answer-key confinement ------------------------------------------------------------

    def find_secrets(self, ws: Workspace, secrets: list[str]) -> list[str]:
        """Files under the trial's directories that contain any of `secrets` (a generated
        answer key or seed must exist only on the host)."""
        script = FIND_SECRETS % {"needles": json.dumps(secrets), "root": ntpath.dirname(ws.state)}
        rc, out, err = self.ssh.run([self.python, "-"], input=script.encode("utf-8"), timeout=300)
        if rc:
            raise RuntimeError(f"secret search failed ({rc}): {err.decode('utf-8', 'replace')}")
        return json.loads(out)
