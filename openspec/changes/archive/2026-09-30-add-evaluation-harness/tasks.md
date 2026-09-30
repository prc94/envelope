# Milestone M1 — Windows-first slice to the first decision

## 1. Setup and spikes

- [x] 1.1 Create the `eval/` layout from design E16 (`uv` project for the runner, standalone `eval/gateway/` and `eval/launcher/` with no dependencies, `tasks/`, `variants/`, `jobs/`, `tests/`, git-ignored `results/`) and extend `scripts/dev.sh` lint/types/test to cover it; ask before installing any dependency; verify `scripts/dev.sh lint`, `types` and `test` pass for `envelope/` and `eval/`
- [x] 1.2 Copy the S5 transcripts and audit logs from `/tmp/envelope-interop` into `eval/tests/fixtures/s5/` with a README noting which files were clobbered by the old naming bug; verify the fixture README lists every copied file and its harness, model and scenario
- [x] 1.3 Qwen Code spike at the pinned version (0.24.5, Linux and the VM): find the incremental transcript mode (chat recording or streaming output), the settings that disable each non-shell tool including `agent` and sub-agents, where it reports exposed tools, how deferred MCP calls are wrapped, and which shell `run_shell_command` uses on Windows; verify by a throwaway run per finding, recorded in design E4
- [x] 1.4 Confirm OpenRouter's current API from its documentation (requesting usage in streams, the usage fields, the provider routing object and `allow_fallbacks`, served model and provider fields, keep-alive comments, in-stream error format, generation lookup); verify by recording field names and examples in design E5

## 2. Model gateway (standard library only)

- [x] 2.1 Implement the Unix-socket admin interface and token registry (issue with `{lane, job, trial, allowed_models, request_cap}`, revoke, trial state query) and 401 rejection of missing, unknown or revoked tokens without forwarding; verify with unit tests for each case
- [x] 2.2 Implement the Chat Completions route to OpenRouter with non-streamed relay and chunk-by-chunk SSE relay, content timing that ignores comment lines, in-stream error detection and usage capture (unknown with reason when absent); verify with tests against a local fake upstream covering first-chunk latency, a keep-alive-only stream and an HTTP 200 stream with an error event
- [x] 2.3 Implement allowed-model enforcement, main/auxiliary classification, provider pinning injection, served-model verification with aliases and the per-trial request cap; verify with fake-upstream tests for a disallowed model, a pinned request, a substituted model and a runaway
- [x] 2.4 Implement the JSONL ledger (fields per the model-gateway spec, optional redacted bodies, never credentials or authorization headers), the trial state signal and the credential scan; verify with tests including a scan that finds a planted key by file and offset without printing it
- [x] 2.5 Bind only to loopback and the Docker bridge address and reject unconfigured API families by name; verify with a test for an Anthropic Messages request and a connection attempt on another interface
- [x] 2.6 Provision the gateway in an unprivileged LXC with `eval/gateway/deploy-lxc.sh` (only with explicit approval at the time; it creates the container, a restricted SSH key and a tunnel unit) and document it in `eval/gateway/README.md`; verify neither the runner's user nor its restricted account on the gateway host can read the key, and that one real OpenRouter request succeeds through the tunnel with `curl` and appears in the ledger

## 3. Task framework

- [x] 3.1 Define the `task.toml` schema (id, version, category, lanes, shells, tags, baseline eligibility, known gaps, modifiable paths, timeout) and the suite loader with the instruction linter and suite hash; verify with unit tests over fixture task directories covering each rejection (missing part, no tags, tool name in a baseline task, bash literal in a Windows baseline task)
- [x] 3.2 Implement host-side generation (per-trial seed, fixture bytes, answer key and manifest kept on the host, per-job generation option), upload that preserves bytes, `collect.py` execution on the target with `python -X utf8`, and host-side judging with generator/collect/judge failures as `infra`; verify with unit tests for pass, fail and collect crash and a byte-comparison test for CP1251 and UTF-16LE fixtures
- [x] 3.3 Implement workspace integrity (modified or deleted fixture files outside declared paths fail; created files reported; `.git/` excluded by default; `scratch/` provided); verify with unit tests for a deleted unrelated file and for a read-only git task after `git status`
- [x] 3.4 Implement the answer-key confinement check (search a trial's target environment for its answer key and seed) and the per-lane coverage report; verify the check finds a deliberately planted key and the coverage report flags a missing area in a fixture suite
- [x] 3.5 Write the task authoring guide in `eval/tasks/README.md` (layout, generation, confinement, wording rules, integrity, tags, known gaps, oracles); verify by authoring the canary task strictly from the guide and loading it without errors

## 4. Runner core

- [x] 4.1 Implement the job file (lanes, targets with declared fingerprints, harnesses with pinned versions, models with pinning, aliases and auxiliaries, arms with tool sets, task selection, profile, decision rules, wall-clock cap), the planner (arm tag filtering, baseline eligibility, target support, randomized interleaved order) and the reproducibility record written before the first trial; verify with unit tests for budget-arm filtering, an unsupported harness/target and a reproducible order from the recorded seed
- [x] 4.2 Implement build variants (patch applied to a temporary source copy, built with `scripts/build.py`, hash recorded); verify with a head/tail variant whose rendered split differs from the default in a unit test and whose hash appears in the reproducibility record
- [x] 4.3 Implement the trial lifecycle against the target interface (token, workspace and state dirs, generate and upload, tool-set check, harness run in the containment unit with timeout, collect, judge, invariants, durable append, token revocation, cleanup) with oracle and canary gates, retries limited to two and the three-in-a-row circuit breaker; verify with tests using a fake target and fake harness covering gate skip, timeout, retry and circuit breaker
- [x] 4.4 Implement the event timeline and attribution per design E9; verify with unit tests for each attribution, including native-shell hang as `tool`, a recovered 502 as `model`, an idle harness as `harness`, a disallowed model as `infra` and conflicting evidence as `indeterminate`
- [x] 4.5 Implement invariants and lever metrics per design E10 (leftovers, created files, no tool call, invented handles, coercions, tool errors, truncation and recovery, hints and next-command change, replacement characters, tool discovery, main-model turns and tokens); verify with unit tests on the S5 fixtures from 1.2
- [x] 4.6 Implement durable results (one line per trial, flushed and synced, schema version) and the post-job credential scan via the gateway; verify that a job killed mid-write yields a report listing one malformed record and that a planted key fails the job

## 5. Windows target

- [x] 5.1 Implement the `ssh` target (commands, byte-preserving copy, reverse tunnels bound to loopback with `ExitOnForwardFailure` and keepalives, tunnel health checks, fingerprint measurement); verify by measuring the VM fingerprint and reaching the gateway from the VM through the tunnel, and by a dropped-tunnel test that pauses the job
- [x] 5.2 Implement the Windows Job Object launcher (suspended start, assign, resume, wait, list leftovers, terminate on exit or timeout) reusing envelope's ctypes approach; verify on the VM with a harness stand-in that leaves a background child (reported and killed, tunnel unaffected) and one that exceeds its timeout (whole tree gone per a Toolhelp32 check)
- [x] 5.3 Implement per-trial isolation on the VM (`C:\eval\trials\<trial>`, `HOME`/`USERPROFILE`, `GIT_CONFIG_GLOBAL`, envelope audit log and spool via `ENVELOPE_*`); verify with an oracle run of the canary task and the answer-key confinement check on the VM

## 6. Qwen Code adapter

- [x] 6.1 Implement the adapter interface and the normalized trajectory writer, including wrapper unwrapping and parsing of transcripts cut off by a kill; verify with unit tests on the S5 Qwen fixtures and on a truncated copy of one
- [x] 6.2 Implement the Qwen Code adapter per design E4 and the spike 1.3 findings (gateway wiring, user-level `mcpServers`, shell-only tool set, exposed-tool report for the tool-set check, incremental transcript); verify end to end on the VM against a fake model served by the gateway's fake upstream that issues one `run` call, then with one real canary trial

## 7. Windows VM preparation (each step only with explicit approval at the time)

- [x] 7.1 Check the VM (settings files, user and machine environment) and the post-13.2 state for a stored OpenRouter key; if found, remove it and rotate the key; verify a search for the old key on the VM finds nothing
- [x] 7.2 Switch the system locale to Russian (OEM 866 / ANSI 1251), enable `LongPathsEnabled`, and configure autologon for the `envelope` user if calibration (8.1) requires the interactive launcher; verify `cmd //c chcp` in git-bash prints 866 and `doctor` reports the code pages
- [x] 7.3 Record the evaluation licence expiry date and the rebuild procedure in `docs/windows-test-vm.md`, take the P2 snapshot and record its name and fingerprint; verify the runner's fingerprint measurement matches the recorded one

## 8. M1 tasks

- [x] 8.1 Write the console calibration task (raw bytes of native tools and git-bash, via the SSH launcher and via a scheduled task in the interactive session) and run it; verify the comparison result is recorded in design E8 and, if they differ, switch Windows trials to the interactive launcher and re-run the canary
- [x] 8.2 Write the Windows canary and the three interop scenarios (basic, truncation recovery via `output`, 40-second command) as tasks with oracles; verify all oracles pass on the VM
- [x] 8.3 Write about eight `output-budget`/head-tail tasks from the realistic core (large git log, trace id across logs, last stack trace, error count in a time window, nonce in a large grep result, progress-bar noise); verify all oracles pass on the VM
- [x] 8.4 Write the two known-gap tasks (CP866 native-tool output in git-bash; an editor-opening `git merge`); verify their oracles pass and a real trial is reported under its gap id

## 9. Report and the first decision

- [x] 9.1 Implement per-cell aggregation (count, Wilson interval, attributed failures, invariant and lever summaries, median turns and tokens, cost, `distinct_sequences`), grouped by harness identity, with known gaps and broken or unavailable cells in their own sections; verify with unit tests on a synthetic results file with known values
- [x] 9.2 Implement paired, task-stratified arm comparisons with a bootstrap over tasks, decision-rule evaluation (met / not met / inconclusive), the exploratory label for screening jobs and secret redaction; verify with synthetic results where the expected verdict of each rule is known
- [x] 9.3 Render the report as Markdown plus a JSON summary from a results directory, including partial jobs; verify by generating it from the interrupted job of 4.6
- [x] 9.4 Run the M1 decision job on the Windows lane (Qwen Code, the three candidate models, default arm plus two output-budget arms and one head/tail variant, decision profile on the M1 tasks, declared decision rules, one-night cap); verify the report evaluates every declared rule, contains no provider key, and record the resulting recommendation in design.md

# Milestone M2 — breadth: Linux lane, baseline, full suite

## 10. Linux lane

- [x] 10.1 Build the pinned Linux trial image (Python 3.12, bash/sh/zsh/fish, git, Qwen Code and OpenCode at pinned versions, envelope mounted per arm) — ask before large downloads — and implement the `docker` target (one container per trial, network limited to the gateway and test servers, leftover listing before removal, image-digest fingerprint); verify `python envelope.pyz doctor` in the image lists all four shells and a container cannot read the gateway key or host SSH keys
- [x] 10.2 Run the Qwen Code adapter on the `docker` target; verify one fake-model and one real canary trial on the Linux lane
- [x] 10.3 Implement the OpenCode adapter per design E4 (gateway provider, `small_model` as auxiliary, per-trial `XDG_*`, `--dir`, tool toggles, exposed-tool report, `--format json` stream); verify one fake-model and one real canary trial on the Linux lane

## 11. Native baseline

- [x] 11.1 Implement native-shell arms for both adapters (only the harness's shell tool plus the shared non-shell set; shell tool calls taken from the trajectory for the timeline); verify with a canary trial per adapter where the audit log is empty and the trajectory shows the native shell call
- [x] 11.2 Add the baseline section of the report (paired success difference, turn and token differences, `tool` and `harness` failures per arm, the "your shell" claim separately on Windows); verify with synthetic results

## 12. Database servers

- [x] 12.1 Start MongoDB and Redis containers bound to loopback and the Docker bridge, with per-trial ACL users and databases created by generators, deleted at cleanup, and prefix garbage collection at job start and end; verify an interrupted trial's objects are removed by the next job and a trial user cannot read another trial's keys
- [x] 12.2 Install `mongosh` and the chosen Windows `redis-cli` build on the VM and admit them to the WDAC policy with a scan-and-merge pass, re-running `Set-RuleOption -Option 3 -Delete` (only with explicit approval at the time); verify both clients connect from git-bash through the tunnels and update the VM fingerprint and snapshot

## 13. Full suite

- [x] 13.1 Write the Linux core tasks (large output, long-running, errors and hints, heavy scripts, cwd/env, git, logs, file wrangling, scripts and grep, Mongo, Redis, interop parity); verify all oracles pass on the Linux lane and the coverage report shows no missing Linux area
- [x] 13.2 Write the Windows git and log-digging tasks (history search, merge, UTF-16LE, CP1251 and rotated logs, multi-file tracing); verify oracles pass on the VM
- [x] 13.3 Write the Windows file-wrangling, scripts and grep tasks (Cyrillic and spaced names, duplicates, CSV, CP1251 conversion, long paths, CRLF pipelines); verify oracles pass on the VM
- [x] 13.4 Write the Windows database tasks (large result sets, authentication, `/`-prefixed arguments, bare client start); verify oracles pass on the VM
- [x] 13.5 Write the Windows edge tasks (CP866 alone and mixed, application-control exit 126, Constrained Language Mode, cmd line limit and shell choice, long-running and stdin prompts, errors and hints, cwd/env); verify oracles pass on the VM and the coverage report shows no missing Windows area

## 14. Integration

- [x] 14.1 Run a screening job on the Linux lane (Qwen Code and OpenCode, three models, default and native arms, 3 trials); verify the report is complete and labelled exploratory, contains no provider key, and the interop parity tasks are comparable with S5
- [x] 14.2 Run a screening job on the Windows lane (Qwen Code, three models, default and native arms, 3 trials, split per model if needed to respect the wall-clock cap); verify the report is complete and known gaps appear under their ids
- [x] 14.3 Retire `scripts/interop.py` and update README and CLAUDE.md (commands, interop and evaluation sections) to point at `eval/`; verify a repository search finds no remaining references and the documented evaluation commands run as written
