"""Build a job's runtime (targets, tunnels, adapters, executors, gates) from its job file."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters import QwenAdapter
from ..adapters.base import Adapter
from ..targets import SSHTarget, WindowsTrialTarget
from ..targets.base import verify_fingerprint
from ..tasks.loader import TaskSuite
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
    scratch = out / "scratch"
    for target_name in job.targets:
        cfg = _table(job, "target", target_name)
        check_licence(target_name, cfg)
        if cfg.get("kind", "ssh") != "ssh" or not cfg.get("windows", True):
            raise JobConfigError(
                f"target {target_name!r}: only Windows ssh targets are wired so far"
            )
        ssh = SSHTarget(
            str(cfg["host"]),
            str(cfg["user"]),
            windows=True,
            python=str(cfg.get("python", "python")),
            tools=tuple(cfg.get("tools", ())),  # type: ignore[arg-type]
        )
        log(f"measuring the fingerprint of {target_name}")
        fingerprints[target_name] = verify_fingerprint(
            target_name,
            ssh,
            dict(cfg.get("fingerprint", {})),  # type: ignore[arg-type]
        ).facts
        root = str(cfg.get("root", "C:\\eval"))
        vm = WindowsTrialTarget(ssh, root=root, python=ssh.python)
        vm.setup()
        remote = {}
        for name, artifact in artifacts.items():
            remote[name] = f"{root}\\envelope-{name}.pyz"
            ssh.put(artifact, remote[name])
        ssh.open_reverse_tunnel(
            int(cfg.get("gateway_local_port", 8787)), int(cfg.get("gateway_port", 18787))
        )
        ssh_targets.append(ssh)
        gateway_url = f"http://127.0.0.1:{int(cfg.get('gateway_port', 18787))}/v1"
        oracle_runner = OracleRunner(
            vm,
            tasks,
            scratch / f"oracle-{target_name}",
            python=ssh.python,
            oracle_script=f"{root}\\launcher\\oracle.py",
            envelope=remote[DEFAULT_VARIANT]
            if DEFAULT_VARIANT in remote
            else next(iter(remote.values())),
            shell=str(cfg.get("shell", "git-bash")),
            job=f"{job.name}-oracle",
        )
        oracles[target_name] = oracle_runner.check()
        for harness_name in job.harnesses:
            hcfg = _table(job, "harness", harness_name)
            for arm in job.arms:
                adapter = _adapter(hcfg, ssh.python, remote[arm.variant or DEFAULT_VARIANT])
                executors[(target_name, harness_name, arm.name)] = TrialExecutor(
                    vm,
                    adapter,
                    gateway,
                    tasks,
                    {arm.name: arm},
                    job=job.name,
                    gateway_url=gateway_url,
                    scratch=scratch / target_name,
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
    if kind != "qwen-code":
        raise JobConfigError(f"harness adapter {kind!r} is not implemented yet")
    return QwenAdapter(
        launch=list(harness["launch"]),
        envelope_command=[python, envelope_pyz],
    )
