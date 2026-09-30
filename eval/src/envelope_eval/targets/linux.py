"""Linux trial target: one Docker container per trial on a remote Docker host reached over SSH.

Workspace paths (`workspace`, `home`, `state`, `audit_log`, `spool`) are `container:path`, the
same shape `docker cp` itself uses, so which container a path belongs to travels with the string
and needs no instance state — a single target instance can serve concurrent trials safely.
Environment values (`HOME`, `ENVELOPE_*`, ...) are real in-container paths, since they become
actual environment variables for the harness process.
"""

from __future__ import annotations

import json
import posixpath
import tempfile
from pathlib import Path

from ..runner.trial import UnitResult, Workspace
from .base import Fingerprint
from .ssh import SSHTarget

LAUNCHER_DIR = Path(__file__).parents[3] / "launcher"

# Runs inside the trial container (docker exec -i ... python3 -), needles baked into the script
# source (python3 - reads its whole stdin as the program, so they cannot travel separately);
# neither a command line nor a file on the target ever holds them.
FIND_SECRETS = """
import json, os

needles = [n.encode("utf-8") for n in json.loads(%(needles)r)]
hits = []
for base, _dirs, files in os.walk("/home/trial"):
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


def _split(pseudo_path: str) -> tuple[str, str]:
    container, _, path = pseudo_path.partition(":")
    if not path:
        raise ValueError(f"not a container-prefixed path: {pseudo_path!r}")
    return container, path


class LinuxTrialTarget:
    """`UnitTarget` for the `docker` lane: every trial gets its own container, started from the
    pinned image on a Docker network with `internal: true` (no route beyond the Docker host — the
    model gateway is reached through a reverse tunnel to that host, and test servers run as
    containers on the same network, both still reachable since neither needs routing out).
    A container is the containment unit itself; `run_unit` additionally uses `launcher/launch.py`
    inside it so leftover detection is the same session-based mechanism as everywhere else, and
    `cleanup` force-removes the container, which reaps anything that mechanism missed."""

    def __init__(
        self,
        ssh: SSHTarget,
        image: str,
        network: str = "envelope-eval",
        python: str = "python3",
    ) -> None:
        self.ssh, self.image, self.network, self.python = ssh, image, network, python
        self._staging = f"/home/{ssh.user}/eval-staging"
        self._launcher = f"/home/{ssh.user}/eval-launcher"

    def _sh(self, command: str, timeout: float = 120) -> tuple[int, bytes, bytes]:
        return self.ssh.run_shell(command, timeout=timeout)

    def _must(self, command: str, what: str, timeout: float = 120) -> bytes:
        rc, out, err = self._sh(command, timeout=timeout)
        if rc:
            raise RuntimeError(f"{what} failed ({rc}): {err.decode('utf-8', 'replace').strip()}")
        return out

    @staticmethod
    def _name(trial_id: str) -> str:
        return f"envelope-eval-{trial_id}"

    # -- once per job ----------------------------------------------------------------------

    def setup(self, envelope_pyz: Path | None = None) -> None:
        """Create the isolated network (idempotent) and stage the launcher files, and optionally
        one `envelope.pyz`, on the host, ready to be copied into each trial's container."""
        self._sh(f"docker network create --internal {self.network}")  # exit 1 if it exists: fine
        self._must(f"mkdir -p {self._staging} {self._launcher}", "mkdir")
        for name in ("launch.py", "oracle.py"):
            self.ssh.put(LAUNCHER_DIR / name, f"{self._launcher}/{name}")
        if envelope_pyz is not None:
            self.ssh.put(envelope_pyz, f"{self._launcher}/envelope.pyz")

    def network_gateway(self) -> str:
        """The trial network's own bridge gateway address (not the default `docker0` bridge's,
        which trial containers cannot reach: they join this `internal` network, task 10.1). The
        model gateway and the shared database containers are reached from inside a trial
        container at this address, not loopback; wiring a job binds its reverse tunnel to it
        rather than to a hardcoded, deployment-specific value."""
        out = self._must(
            f"docker network inspect {self.network} "
            "--format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'",
            "network inspect",
        )
        gateway = out.decode().strip()
        if not gateway:
            raise RuntimeError(f"docker network {self.network!r} has no gateway address")
        return gateway

    # -- UnitTarget ------------------------------------------------------------------------

    def prepare(self, trial_id: str) -> Workspace:
        name = self._name(trial_id)
        self._sh(f"docker rm -f {name}")  # a leftover container from an earlier attempt
        self._must(
            f"docker run -d --name {name} --network {self.network} --network-alias {trial_id} "
            f"--cpus 2 --memory 2g --pids-limit 512 --user trial {self.image} sleep infinity",
            "container start",
        )
        staged = ("launch.py", "oracle.py")
        rc, _, _ = self._sh(f"test -f {self._launcher}/envelope.pyz")
        if rc == 0:
            staged = (*staged, "envelope.pyz")
        for filename in staged:
            self._must(
                f"docker cp {self._launcher}/{filename} {name}:/home/trial/{filename}",
                "install launcher",
            )
        base = "/home/trial"
        home, state, spool = f"{base}/home", f"{base}/state", f"{base}/spool"
        self._must(
            f"docker exec {name} sh -c 'mkdir -p {home} {state} {spool}'", "prepare directories"
        )
        return Workspace(
            workspace=f"{name}:{base}/workspace",
            home=f"{name}:{home}",
            state=f"{name}:{state}",
            audit_log=f"{name}:{state}/audit.jsonl",
            spool=f"{name}:{spool}",
            env={
                "HOME": home,
                "XDG_STATE_HOME": f"{home}/.local/state",
                "XDG_CACHE_HOME": f"{home}/.cache",
                "XDG_CONFIG_HOME": f"{home}/.config",
                "GIT_CONFIG_GLOBAL": f"{home}/.gitconfig",
                "ENVELOPE_AUDIT_LOG": f"{state}/audit.jsonl",
                "ENVELOPE_SPOOL_DIR": spool,
            },
        )

    def real_path(self, remote: str) -> str:
        return _split(remote)[1]

    def upload(self, local: Path, remote: str) -> None:
        container, path = _split(remote)
        staged = f"{self._staging}/{Path(path).name}-{id(local):x}"
        self.ssh.put(local, staged)
        # A directory source ending in "/." makes docker cp merge its contents into an existing
        # destination directory; without it, docker cp nests the source under the destination
        # instead (the destination here, `path`, is always pre-created by `prepare`'s mkdir).
        source = f"{staged}/." if local.is_dir() else staged
        self._must(f"docker cp {source} {container}:{path}", "upload")
        self._sh(f"rm -rf {staged}")

    def download(self, remote: str, local: Path) -> None:
        container, path = _split(remote)
        staged = f"{self._staging}/dl-{id(local):x}"
        rc, _, err = self._sh(f"docker cp {container}:{path} {staged}")
        if rc:
            text = err.decode("utf-8", "replace")
            lowered = text.lower()
            if (
                "no such container:path" in lowered
                or "could not find the file" in lowered
                or "not found" in lowered
            ):
                raise FileNotFoundError(remote)
            raise RuntimeError(f"download of {remote} failed ({rc}): {text}")
        self.ssh.get(staged, local)
        self._sh(f"rm -rf {staged}")

    def run_unit(
        self, argv: list[str], *, env: dict[str, str], cwd: str, timeout: float, stdout_to: str
    ) -> UnitResult:
        container, real_cwd = _split(cwd)
        _, real_stdout = _split(stdout_to)
        state = posixpath.dirname(real_stdout)
        spec = {
            "argv": argv,
            "cwd": real_cwd,
            "env": env,
            "timeout": timeout,
            "stdout": real_stdout,
            "result": f"{state}/launch_result.json",
        }
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "spec.json"
            local.write_text(json.dumps(spec), encoding="utf-8")
            self.upload(local, f"{container}:{state}/spec.json")
        rc, out, err = self.ssh.run(
            [
                "docker",
                "exec",
                container,
                self.python,
                "/home/trial/launch.py",
                f"{state}/spec.json",
            ],
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
        container, real_state = _split(ws.state)
        _, real_workspace = _split(ws.workspace)
        remote = f"{real_state}/collect.py"
        self.upload(script, f"{container}:{remote}")
        env_flags = " ".join(f"-e {k}={v}" for k, v in ws.env.items() if k.startswith("ENVELOPE_"))
        argv = f"docker exec -w {real_workspace} {env_flags} {container} {self.python} -X utf8 {remote}"
        return self.ssh.run(argv.split(), timeout=timeout)

    def cleanup(self, ws: Workspace) -> None:
        container, _ = _split(ws.state)
        self._sh(f"docker top {container}")  # a last look before force-removing it (best effort)
        self._sh(f"docker rm -f {container}")

    # -- answer-key confinement ------------------------------------------------------------

    def find_secrets(self, ws: Workspace, secrets: list[str]) -> list[str]:
        container, _ = _split(ws.state)
        script = FIND_SECRETS % {"needles": json.dumps(secrets)}
        rc, out, err = self.ssh.run(
            ["docker", "exec", "-i", container, self.python, "-"],
            input=script.encode("utf-8"),
            timeout=300,
        )
        if rc:
            raise RuntimeError(f"secret search failed ({rc}): {err.decode('utf-8', 'replace')}")
        return json.loads(out)

    # -- fingerprint -----------------------------------------------------------------------

    def fingerprint(self) -> Fingerprint:
        digest = self._must(f"docker inspect --format='{{{{.Id}}}}' {self.image}", "image inspect")
        version = self._must("docker version --format '{{.Server.Version}}'", "docker version")
        return Fingerprint(
            {"image_digest": digest.decode().strip(), "docker_version": version.decode().strip()}
        )
