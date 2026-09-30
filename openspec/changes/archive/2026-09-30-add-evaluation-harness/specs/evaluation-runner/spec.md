# Spec Delta

## Purpose

Runs the evaluation task suite through real agent harnesses and models on real targets, isolates, contains and scores every trial, and reports results that can decide envelope's defaults and quantify its value over native shell tools.

## ADDED Requirements

### Requirement: Matrix as data
The runner SHALL plan trials over the axes lane, harness, model, arm, task and trial index, read from a job file. Adding a model, arm or task SHALL require only configuration or task files; adding a harness SHALL require only a new harness adapter; adding a target operating system SHALL require only a target definition and adapter support. The planner SHALL randomize and interleave trial order across arms from a recorded order seed.

#### Scenario: New model without code change
- **WHEN** a user adds a model id to the job file
- **THEN** the next job schedules trials for that model with no code change

#### Scenario: Interleaved arms
- **WHEN** a job has two arms on the same tasks
- **THEN** trials of both arms alternate through the job in a randomized order that the job records

### Requirement: Targets and fingerprints
The runner SHALL execute trials on targets of two kinds: `ssh` (a real host reached over SSH, used for the Windows test VM and later macOS) and `docker` (one container per trial on the Linux host, from a pinned image). Each target SHALL declare a fingerprint: for `ssh` targets the declared snapshot name, OS build, system locale and code pages, harness versions, and hashes of the relevant binaries and application-control policies; for `docker` targets the image digest. Before a job starts on a target, the runner SHALL measure the fingerprint and refuse to run on a mismatch with the job's declaration.

#### Scenario: Drifted VM refused
- **WHEN** a Windows job declares code page 866 and the VM reports 437
- **THEN** the job does not start on that target and reports the mismatch

#### Scenario: Linux trial isolated from host secrets
- **WHEN** a Linux-lane agent searches the filesystem it can reach
- **THEN** it cannot read the gateway's key, SSH keys or any file of the runner host outside its container

### Requirement: Harness adapters
A harness adapter SHALL: declare the targets it supports; register envelope as a stdio MCP server in user-level configuration with the arm's settings; configure the arm's tool set; direct all model traffic to the gateway with the trial's token; run the harness headless; produce an incrementally written transcript that survives the harness being killed; convert it into a normalized trajectory (steps, tool calls with arguments and results, final answer, harness errors), unwrapping any generic tool-invocation wrapper to the underlying tool name; and report the harness name and version, which identify the client in results (cross-checked with the MCP client identity in envelope's audit log). A Qwen Code adapter SHALL be provided for `ssh` and `docker` targets and an OpenCode adapter for `docker` targets.

#### Scenario: Killed harness still has a trajectory
- **WHEN** a trial times out and the harness is killed
- **THEN** the trajectory contains every step and tool call recorded before the kill

#### Scenario: Wrapped tool call
- **WHEN** Qwen Code invokes envelope's `run` through its deferred-tool wrapper
- **THEN** the trajectory records a call to envelope's `run`, not to the wrapper

#### Scenario: Unsupported target
- **WHEN** a job lists OpenCode for the Windows lane
- **THEN** planning reports the combination as unsupported and schedules no trials for it

### Requirement: Tool sets
Each arm SHALL define the complete tool set exposed to the model: the shell tool (envelope's tools in envelope arms, the harness's native shell tool in the baseline) and the non-shell harness tools. The non-shell tool set SHALL be identical across arms of a job and SHALL default to none (file reading, searching, editing, web access and sub-agents disabled). Before each trial's agent phase, the runner SHALL verify from the harness's own report of available tools that exactly the configured tool set is exposed; a mismatch SHALL make the trial `infra` and SHALL NOT be scored.

#### Scenario: Built-in grep disabled
- **WHEN** a trial runs a log-digging task in an envelope arm with the default tool set
- **THEN** the harness exposes no file-search tool and the model can only solve the task through envelope

#### Scenario: Unexpected tool exposed
- **WHEN** a harness update re-enables a file-reading tool despite the configuration
- **THEN** the tool-set check fails, the trial is `infra`, and the report names the unexpected tool

### Requirement: Trial isolation and containment
Each trial SHALL have its own workspace, harness state and home directories, envelope audit log and spool, and gateway token; concurrent trials SHALL NOT share any of these. The harness and every process it starts SHALL run inside a containment unit — a Windows Job Object on `ssh` Windows targets, the trial's container on `docker` targets — so that processes still alive after the harness exits are identified exactly (the leftover-process invariant) and killed, and a timeout terminates the whole unit. The runner's own processes (tunnels, collect, snapshots) SHALL run outside the unit.

#### Scenario: Leftover process detected
- **WHEN** a trial's command starts a background process that survives the harness
- **THEN** the trial record lists it as a leftover, the runner terminates it, and the SSH tunnel is unaffected

#### Scenario: Timeout kills the unit
- **WHEN** a trial exceeds its timeout
- **THEN** every process in the trial's containment unit is terminated and the trial is recorded as timed out

### Requirement: Tunnel health
On `ssh` targets the runner SHALL maintain the reverse tunnels to the gateway and test servers for the session and check them before and after each trial. A tunnel that is down SHALL make the trial `infra` and SHALL pause the job until it is restored or the job is aborted.

#### Scenario: Dropped tunnel
- **WHEN** the tunnel drops during a trial
- **THEN** the trial is `infra`, no further trials start until the tunnel check passes again

### Requirement: Arms
An arm SHALL be either an envelope variant (a set of envelope settings, optionally a build variant of `envelope.pyz` for wordings or constants such as the head/tail split) or a native-shell baseline, together with its tool set. Each arm SHALL declare the lever tags it affects; non-default envelope arms SHALL be scheduled only on tasks carrying one of those tags, and the baseline arm only on tasks eligible for the baseline on that lane. The default arm SHALL run on every scheduled task.

#### Scenario: Budget arm restricted to relevant tasks
- **WHEN** an arm varies the output budget and declares the tag `output-budget`
- **THEN** it is scheduled only on tasks tagged `output-budget`, while the default arm runs on all tasks

### Requirement: Gates before model trials
On each target the runner SHALL run task oracles before model trials, then canary tasks for each harness and model. If the canary trials of a harness and model fail for `infra` reasons, the runner SHALL skip that cell's remaining trials and report the cell as unavailable with the canary failures.

#### Scenario: Broken provider route
- **WHEN** all canary trials for one model fail with unrecovered upstream errors
- **THEN** that model's remaining trials on the target are skipped and the report shows the model as unavailable

### Requirement: Event timeline
For every trial the runner SHALL assemble a timeline from the gateway ledger (request start, first and last content, end reason, in-stream errors), shell tool calls (start and end from envelope's audit log in envelope arms, from the trajectory in the baseline), harness events and exit, and the collect and judge results, and SHALL keep it with the trial record.

#### Scenario: Tool call in flight at timeout
- **WHEN** a trial times out while an envelope `run` call has started but not finished
- **THEN** the timeline shows the open tool call at the timeout

### Requirement: Failure attribution
Every failed trial SHALL receive exactly one attribution, decided from its timeline:
- `infra`: generator, upload, collect or judge failure; tool-set or fingerprint mismatch; tunnel down; a request for a model the trial's token does not allow; an upstream failure that was not recovered (the last model request failed, or the upstream stream stayed idle beyond the stall threshold).
- `tool`: the arm's shell tool failed — a timeout while a shell tool call was in flight, a tool crash, an envelope tool error that is not a rejection of invalid input, or a context-length rejection following a tool result.
- `harness`: a harness error or crash not tied to a shell tool call, or no model request and no tool call in flight beyond the stall threshold.
- `model`: a failed verdict otherwise, including runaway loops stopped by the gateway's request cap and malformed tool calls rejected by the harness or envelope.
- `indeterminate`: evidence points to more than one of the above.
An upstream error that the harness recovered from SHALL NOT change the attribution. Comparisons SHALL exclude only `infra` and `indeterminate` trials; `tool` and `harness` failures count as failures and SHALL be broken out per arm.

#### Scenario: Native shell hang counts against the native arm
- **WHEN** in the baseline arm the native shell tool hangs on an editor-opening git command until the trial times out
- **THEN** the trial is attributed to `tool` and counts as a failure of the baseline arm

#### Scenario: Recovered provider error
- **WHEN** a trial sees one upstream 502 that the harness retried successfully and then fails its verdict
- **THEN** the trial is attributed to `model`, not `infra`

#### Scenario: Harness stall
- **WHEN** a Qwen Code trial has no model request and no tool call in flight for longer than the stall threshold and then times out
- **THEN** the trial is attributed to `harness`, counts as a failure, and appears in the harness-stall column

### Requirement: Retries
Only `infra` trials MAY be retried, at most twice per trial; a retry SHALL replace the attempt, the record SHALL keep the retry count and reasons, and a cell SHALL stop after three consecutive `infra` trials. A stopped cell SHALL be probed again after a cool-down (a canary gate, then one trial that trips the breaker again at once if it fails); its trials SHALL be deferred, not skipped, and run when the cell recovers, and SHALL be recorded as unavailable only if it has not recovered by the end of the job or a bounded wait after the plan.

#### Scenario: Circuit breaker
- **WHEN** three consecutive trials of one cell are `infra`
- **THEN** the cell's remaining trials are not run for now and are deferred

#### Scenario: Provider recovers
- **WHEN** a model's provider was rate-limited at the start and answers again after the cool-down
- **THEN** the cell's deferred trials run, no unavailable record is written for them, and the report shows them as ordinary results

#### Scenario: Provider does not recover
- **WHEN** the cell is still down after the bounded wait at the end of the plan
- **THEN** each of its trials is recorded once as unavailable and the report shows the cell as unavailable

### Requirement: Invariants and lever metrics
For every trial, independently of the verdict, the runner SHALL record: leftover processes; fixture files created by the agent; whether the model answered without any tool call; tool calls referencing output handles envelope never issued; argument coercions and tool errors from envelope's audit log; for each shell tool result, whether it was truncated, whether the model then retrieved omitted output through `output` and whether that succeeded, which hints were shown and whether the next command changed, and the number of replacement characters; the number of tool-discovery calls before the first shell tool call and whether the first discovery call returned the shell tool, reported separately from model turns; and main-model turns and tokens from the gateway.

#### Scenario: Truncation recovery recorded
- **WHEN** a `run` result is truncated and the model then calls `output` with a grep that returns the needed line
- **THEN** the trial record shows one truncated result followed by one successful recovery

### Requirement: Reproducibility record
Before its first trial, a job SHALL write to its result directory the resolved job file and its hash, the suite hash and task versions, envelope and variant build hashes, harness and adapter versions (pinned per job), target fingerprints, model ids with provider pinning, sampling parameters and the order seed. A job SHALL have a wall-clock cap after which no new trials start.

#### Scenario: Re-run with the same inputs
- **WHEN** a user compares two jobs' records
- **THEN** every input that differs between them is visible in the records

### Requirement: Durable results
Each trial record SHALL be appended as one line and flushed to stable storage when the trial finishes, and SHALL carry a schema version. Report generation SHALL work on partial jobs and SHALL report, not silently drop, malformed records.

#### Scenario: Interrupted job
- **WHEN** a job is killed after 40 of 120 trials, in the middle of writing the 41st record
- **THEN** the report is generated from the 40 complete records and lists one malformed record

### Requirement: Report and decisions
The report SHALL show per cell (task x harness x model x arm on a lane) the trial count, success rate with a 95% Wilson interval (descriptive), attributed failures, invariant and lever-metric summaries, median turns and tokens, cost and the number of distinct tool-call sequences; per-arm and per-lane aggregates; known-gap results separately; and broken or unavailable tasks and cells. Comparisons between arms SHALL be paired and stratified by task, with uncertainty estimated by resampling tasks. A job MAY declare decision rules per lever (for example a non-inferiority margin on success and a minimum reduction in tokens); the report SHALL evaluate each declared rule and state whether it is met, not met or inconclusive. Jobs using the screening profile SHALL be labelled exploratory and SHALL NOT evaluate decision rules.

#### Scenario: Declared decision rule
- **WHEN** a job declares that a budget arm is adopted if its success rate is not worse than the default arm by more than 5 points and median tokens fall by at least 15%
- **THEN** the report states met, not met or inconclusive, with the paired estimate and interval behind it

#### Scenario: Baseline comparison
- **WHEN** a job ran the default arm and the native baseline on the same baseline-eligible tasks
- **THEN** the report shows the paired success difference with its interval and the turn and token differences, with `tool` and `harness` failures broken out per arm

### Requirement: Secrets in artifacts
The provider key SHALL never appear in job files, trial records, transcripts, trajectories, ledgers or logs; after each job the gateway's credential scan SHALL be run over the result directory and a finding SHALL fail the job. Per-trial secrets (gateway tokens, database users and passwords) SHALL be issued per trial, revoked or deleted when the trial ends, and redacted from generated reports.

#### Scenario: Database password in the audit log
- **WHEN** a model runs `redis-cli -a <password>` and the password appears in envelope's audit log
- **THEN** the password belongs to a Redis user deleted at the end of the trial and is redacted in the report

### Requirement: Interop scenarios preserved
The runner SHALL reproduce the three scenarios of `scripts/interop.py` (basic command, truncation recovery through the output tool, a 40-second command) as tasks before that script is removed.

#### Scenario: Interop parity
- **WHEN** the three interop tasks run for OpenCode and Qwen Code on the Linux lane
- **THEN** each has a verdict and the report can be compared with the S5 results
