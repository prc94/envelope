## Why

Envelope's defaults (output budget, head/tail split, tool descriptions, hints) were chosen by reasoning, and its value over a harness's native shell tool has only been shown by nine smoke runs (`scripts/interop.py`, S5). Before P3 builds on those choices, we need a repeatable way to measure how open models actually use envelope in real harnesses — above all Qwen Code in git-bash on a locked-down Windows 10 host with a Russian code page (OEM 866), which is the primary corp workload — and to turn those measurements into decisions.

## What Changes

- New evaluation harness in `eval/` (dev tooling, not shipped in `envelope.pyz`; not bound by the stdlib-only rule) that runs a task suite through real agent harnesses and scores trials from final state, envelope's audit log, harness transcripts and recorded model traffic.
- **One host runner, two target kinds:** `ssh` targets (the Windows test VM with real AppLocker/WDAC/CLM enforcement and a Russian system locale; macOS later) and `docker` targets (Linux, one container per trial so agents cannot reach host secrets). The same lifecycle, adapters, results and report serve every lane.
- **Task suite** with host-side fixture generation, collect-on-target / judge-on-host checkers, oracles and lever/claim tags. Realistic core from corp workloads (git, log digging, file wrangling, scripts/grep, MongoDB and Redis CLIs from git-bash) plus edge categories (large output, long-running commands, errors and hints, heavy scripts, Windows shells and encodings, lockdown, cwd/env, envelope mechanics).
- **Arms:** envelope variants (settings, or a build variant of `envelope.pyz` for wordings and the head/tail split) and a native-shell baseline, each with an explicit, identical non-shell tool set.
- **Model gateway:** a standard-library-only recording proxy in front of OpenRouter, run as its own process and OS user, giving every harness one endpoint and a per-trial local token bound to the allowed models; the real key never leaves it.
- **Sound scoring:** symmetric failure attribution (`model` / `tool` / `harness` / `infra` / `indeterminate`) from a per-trial event timeline; lever metrics (truncation, recovery through `output`, hint effect, replacement characters, tool discovery); paired, task-stratified comparisons evaluated against decision rules declared in the job; reproducibility record and target fingerprints.
- **Two milestones:** M1 is a Windows-first end-to-end slice (gateway, runner with `ssh` target, Qwen Code adapter, about 15 tasks) ending in the first real decision — output budget and head/tail split. M2 adds the `docker` Linux lane, the OpenCode adapter, the native baseline, the full suite, screening jobs and the retirement of `scripts/interop.py`.
- Extensible axes: lane, target, harness, model, arm, task and trial are data; harness adapters and gateway API families (Chat Completions now; Anthropic Messages and OpenAI Responses later) are plug-ins, so Claude Code and Codex can be added without redesign.

Out of scope: comparing tool surfaces A/B/C (moved to P3a, where `input`/`stop` exist — this change only makes arms a first-class axis); fixing product issues the suite exposes (git-bash has no code-page fallback at all, and there is no `GIT_EDITOR` default — both recorded as known-gap baselines and fixed in a follow-up change); adding `truncated`/`replaced` fields to envelope's audit outcome (derived from transcripts here; a possible small P1 follow-up); Harbor integration (a possible later scale-out); CI; the macOS lane; Claude Code / Codex adapters.

## Capabilities

### New Capabilities
- `evaluation-tasks`: Task definition, host-side fixture generation, answer-key confinement, instruction rules and linting, collect/judge checkers, workspace integrity, oracles, lever/claim tags, known gaps and required suite coverage.
- `evaluation-runner`: Targets and fingerprints, harness adapters and tool sets, trial isolation and process containment, arms and planning, gates, event timeline and failure attribution, retries, invariants and lever metrics, reproducibility, durable results, the report and decision rules, and secret handling.
- `model-gateway`: The recording proxy between harnesses and model providers — isolation of the provider key, per-trial tokens bound to allowed models, API-family routing, streaming semantics, request recording, provider pinning, served-model checks, stall states, loop guard, network exposure and credential scanning.

### Modified Capabilities
<!-- None: envelope's own behaviour does not change in this change. Product fixes the suite motivates are proposed separately. -->

## Impact

- New top-level `eval/` project run with `uv` (runner, adapters, tasks, report); the gateway is a separate standard-library-only program with its own configuration, run under a dedicated OS user. Nothing under `envelope/` changes and `envelope.pyz` stays dependency-free.
- Linux host: Docker (already installed) for per-trial Linux containers and for MongoDB/Redis servers, all bound to loopback or the Docker bridge; the gateway process holding the OpenRouter key.
- Windows test VM (`docs/windows-test-vm.md`): Russian system locale (OEM 866 / ANSI 1251), long paths enabled, a trial root outside `%TEMP%`, `mongosh` and a Windows `redis-cli` admitted by WDAC, a check for (and rotation of) any OpenRouter key stored by earlier tests, SSH reverse tunnels for the gateway and databases, and a recorded P2 snapshot. Each installation, VM or host system change requires explicit approval at the time it is made.
- `scripts/interop.py` retired in M2 once its three scenarios run as tasks.
- OpenRouter usage (budget managed on the OpenRouter side).
