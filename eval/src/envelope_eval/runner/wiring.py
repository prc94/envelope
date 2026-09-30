"""Build a job's runtime (targets, tunnels, adapters, executors, gates) from its job file."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters import OpenCodeAdapter, QwenAdapter
from ..adapters.base import Adapter
from ..targets import LinuxTrialTarget, SSHTarget, WindowsTrialTarget
from ..targets.base import verify_fingerprint
from ..tasks.loader import TaskSuite
from .databases import DEFAULT_CONFIG as DEFAULT_DB_CONFIG
from .databases import DatabaseAdmin, admin_from_config
from .driver import ExecutorKey, Runtime
from .gateway import client_from_config
from .lifecycle import GateFailure, Gates
from .oracle import OracleRunner
from .planner import Job, Trial
from .results import ResultWriter
from .trial import TrialExecutor
from .variants import build_variant

DEFAULT_VARIANT = "default"


class JobConfigError(ValueError):
    pass


def expand_tasks(
    selectors: tuple[str, ...], suite: TaskSuite, lanes: tuple[str, ...]
) -> tuple[str, ...]:
    """Task ids, or `tag:<name>` for every task carrying that tag, limited to the job's lanes."""
    known = {t.id: t for t in suite.tasks}
    chosen: list[str] = []
    for selector in selectors:
        if selector.startswith("tag:"):
            matches = [t.id for t in suite.tasks if selector[4:] in t.tags]
            if not matches:
                raise JobConfigError(f"no task has the tag {selector[4:]!r}")
            chosen += matches
        elif selector in known:
            chosen.append(selector)
        else:
            raise JobConfigError(f"unknown task {selector!r}")
    unique = list(dict.fromkeys(chosen))
    return tuple(t for t in unique if set(lanes) & set(known[t].lanes))


def check_licence(
    target: str, cfg: dict[str, Any], today: date | None = None, margin: int = 3
) -> None:
    """Refuse to start on a target whose evaluation licence ends within `margin` days: an
    expired evaluation Windows powers off on a schedule and would corrupt a running job."""
    expires = cfg.get("expires")
    if expires is None:
        return
    end = expires if isinstance(expires, date) else date.fromisoformat(str(expires))
    today = today or datetime.now(UTC).date()
    if today >= end - timedelta(days=margin):
        raise JobConfigError(
            f"target {target!r}: the evaluation licence ends on {end} "
            f"(refusing to start within {margin} days of it; rebuild the VM, see docs/windows-test-vm.md)"
        )


def _table(job: Job, kind: str, name: str) -> dict[str, Any]:
    try:
        return dict(job.extra[kind][name])  # type: ignore[arg-type]
    except KeyError:
        raise JobConfigError(f"the job has no [{kind}.{name}] table") from None


def build_runtime(
    job: Job,
    suite: TaskSuite,
    repo: Path,
    out: Path,
    *,
    log: Callable[[str], None] = lambda message: None,
) -> Runtime:
    """Connect to every target, verify fingerprints and the model allowlist, build the envelope
    variants, open the tunnels and assemble one executor per (target, harness, arm)."""
    tasks = {t.id: t for t in suite.tasks}
    gateway_cfg = dict(job.extra.get("gateway", {}))
    gateway = (
        client_from_config(str(gateway_cfg["config"]))
        if "config" in gateway_cfg
        else client_from_config()
    )

    verdict = gateway.models(check=list(job.models))
    if verdict.get("blocked"):
        raise JobConfigError(f"models blocked by the key's guardrail: {verdict['blocked']}")

    variants: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, Path] = {}
    wanted = {arm.variant or DEFAULT_VARIANT for arm in job.arms}
    for name in sorted(wanted):
        patch = None
        if name != DEFAULT_VARIANT:
            patch = repo / str(_table(job, "variant", name)["patch"])
        log(f"building envelope variant {name}")
        built = build_variant(repo, patch, name=name, output=out / "variants" / f"{name}.pyz")
        variants[name] = {"source_hash": built.source_hash, "artifact_hash": built.artifact_hash}
        artifacts[name] = built.artifact

    executors: dict[ExecutorKey, Callable[[Trial], dict[str, Any]]] = {}
    fingerprints: dict[str, Any] = {}
    ssh_targets: list[SSHTarget] = []
    oracles: dict[str, Callable[[Trial], tuple[bool, str]]] = {}
    dbs: dict[str, DatabaseAdmin] = {}
    scratch = out / "scratch"
    for target_name in job.targets:
        cfg = _table(job, "target", target_name)
        check_licence(target_name, cfg)
        kind = cfg.get("kind", "ssh")
        if kind == "ssh" and cfg.get("windows", True):
            unit, ssh, gateway_url, remote, fp_target = _build_windows_target(
                job, cfg, target_name, artifacts, out, log
            )
        elif kind == "docker":
            unit, ssh, gateway_url, remote, fp_target, db = _build_linux_target(
                job, cfg, target_name, artifacts, out, log
            )
            if db is not None:
                dbs[target_name] = db
        else:
            raise JobConfigError(
                f"target {target_name!r}: only Windows ssh and Linux docker targets are wired so far"
            )
        fingerprints[target_name] = verify_fingerprint(
            target_name,
            fp_target,
            dict(cfg.get("fingerprint", {})),  # type: ignore[arg-type]
        ).facts
        ssh_targets.append(ssh)
        default_shell = "bash" if kind == "docker" else "git-bash"
        oracle_runner = OracleRunner(
            unit,
            tasks,
            scratch / f"oracle-{target_name}",
            python=ssh.python,
            oracle_script=remote["oracle_script"],
            envelope=remote.get(DEFAULT_VARIANT) or next(iter(remote.values())),
            shell=str(cfg.get("shell", default_shell)),
            job=f"{job.name}-oracle",
            db=dbs.get(target_name),
        )
        oracles[target_name] = oracle_runner.check()
        for harness_name in job.harnesses:
            hcfg = _table(job, "harness", harness_name)
            for arm in job.arms:
                adapter = _adapter(hcfg, ssh.python, remote[arm.variant or DEFAULT_VARIANT])
                # OpenCode's title agent (small_model, E4) uses the *same* model id as the main
                # request, so declaring it here as its own auxiliary_models entry would classify
                # every request for that model as auxiliary (`is_auxiliary`'s model-id check short
                # -circuits before its own `saw_tools and not body.get("tools")` fallback, which is
                # what actually tells the two apart for a same-model case): zeroes out
                # main_model_turns/tokens for every trial instead of just the tool-less one.
                # auxiliary_models is for a *different* auxiliary model id; OpenCode needs none.
                executors[(target_name, harness_name, arm.name)] = TrialExecutor(
                    unit,
                    adapter,
                    gateway,
                    tasks,
                    {arm.name: arm},
                    job=job.name,
                    gateway_url=gateway_url,
                    scratch=scratch / target_name,
                    db=dbs.get(target_name),
                )

    def execute(trial: Trial) -> dict[str, Any]:
        return executors[(trial.target, trial.harness, trial.arm)](trial)

    def oracle(trial: Trial) -> tuple[bool, str]:
        return oracles[trial.target](trial)

    gates: Callable[[Trial], GateFailure | None] = Gates(
        oracle, execute, writer=ResultWriter(out / "gates.jsonl")
    )

    def health() -> list[str]:
        return [problem for ssh in ssh_targets for problem in ssh.tunnel_health()]

    def restore() -> None:
        for ssh in ssh_targets:
            ssh.restore_tunnels()

    def close() -> None:
        for ssh in ssh_targets:
            ssh.close_tunnels()
        for target_name, db in dbs.items():
            log(f"garbage-collecting database objects for {job.name} on {target_name}")
            db.gc(job.name)

    return Runtime(
        executors=executors,
        scan=gateway,
        gates=gates,
        health=health,
        restore=restore,
        provenance={
            "fingerprints": fingerprints,
            "variants": variants,
            "models": {"checked": list(job.models), "allowed": verdict.get("allowed", [])},
            "built_at": time.time(),
        },
        close=close,
    )


def _adapter(harness: dict[str, Any], python: str, envelope_pyz: str) -> Adapter:
    kind = harness.get("adapter", "qwen-code")
    if kind == "qwen-code":
        return QwenAdapter(
            launch=list(harness["launch"]),
            envelope_command=[python, envelope_pyz],
        )
    if kind == "opencode":
        return OpenCodeAdapter(
            launch=list(harness["launch"]),
            envelope_command=[python, envelope_pyz],
        )
    raise JobConfigError(f"harness adapter {kind!r} is not implemented yet")


def _build_windows_target(
    job: Job,
    cfg: dict[str, Any],
    target_name: str,
    artifacts: dict[str, Path],
    out: Path,
    log: Callable[[str], None],
) -> tuple[WindowsTrialTarget, SSHTarget, str, dict[str, str], SSHTarget]:
    ssh = SSHTarget(
        str(cfg["host"]),
        str(cfg["user"]),
        windows=True,
        python=str(cfg.get("python", "python")),
        tools=tuple(cfg.get("tools", ())),  # type: ignore[arg-type]
    )
    log(f"measuring the fingerprint of {target_name}")
    root = str(cfg.get("root", "C:\\eval"))
    vm = WindowsTrialTarget(ssh, root=root, python=ssh.python)
    vm.setup()
    remote = {}
    for name, artifact in artifacts.items():
        remote[name] = f"{root}\\envelope-{name}.pyz"
        ssh.put(artifact, remote[name])
    remote["oracle_script"] = f"{root}\\launcher\\oracle.py"
    ssh.open_reverse_tunnel(
        int(cfg.get("gateway_local_port", 8787)), int(cfg.get("gateway_port", 18787))
    )
    gateway_url = f"http://127.0.0.1:{int(cfg.get('gateway_port', 18787))}/v1"
    return (
        vm,
        ssh,
        gateway_url,
        remote,
        ssh,
    )  # Windows: fingerprint the raw host, not WindowsTrialTarget


def _build_linux_target(
    job: Job,
    cfg: dict[str, Any],
    target_name: str,
    artifacts: dict[str, Path],
    out: Path,
    log: Callable[[str], None],
) -> tuple[
    LinuxTrialTarget, SSHTarget, str, dict[str, str], LinuxTrialTarget, DatabaseAdmin | None
]:
    """A `docker` target: one trial container per trial on a remote Docker host, reached the same
    way the Windows lane's model gateway is -- a reverse tunnel, but bound to the trial network's
    own bridge gateway address (task 10.1), since a trial container has no route to the runner
    host's own loopback. `LinuxTrialTarget` supports only one envelope build at a time (it always
    copies a fixed `envelope.pyz` into every container, task 13's live verifications never needed
    more than the default variant): a job with a non-default variant on a docker target is refused
    rather than silently running the wrong build."""
    if set(artifacts) - {DEFAULT_VARIANT}:
        raise JobConfigError(
            f"target {target_name!r}: docker targets only support the default envelope variant "
            f"(LinuxTrialTarget copies one fixed envelope.pyz into every trial container)"
        )
    ssh = SSHTarget(
        str(cfg["host"]),
        str(cfg["user"]),
        windows=False,
        python=str(cfg.get("python", "python3")),
        key=cfg.get("key"),
        known_hosts=cfg.get("known_hosts"),
    )
    log(f"measuring the fingerprint of {target_name}")
    image = str(cfg["image"])
    network = str(cfg.get("network", "envelope-eval"))
    linux = LinuxTrialTarget(ssh, image, network=network, python=ssh.python)
    linux.setup(envelope_pyz=artifacts.get(DEFAULT_VARIANT))
    remote = {
        DEFAULT_VARIANT: "/home/trial/envelope.pyz",
        "oracle_script": "/home/trial/oracle.py",
    }
    bridge = linux.network_gateway()
    gateway_port = int(cfg.get("gateway_port", 8787))
    ssh.open_reverse_tunnel(int(cfg.get("gateway_local_port", 8787)), gateway_port, bind=bridge)
    gateway_url = f"http://{bridge}:{gateway_port}/v1"
    db = None
    if cfg.get("databases"):
        db = admin_from_config(ssh, cfg.get("database_config", DEFAULT_DB_CONFIG))
        log(f"starting shared database servers for {target_name}")
        db.start(network)
        db.gc(job.name)  # an earlier interrupted run of this same job name, if any
    return linux, ssh, gateway_url, remote, linux, db
