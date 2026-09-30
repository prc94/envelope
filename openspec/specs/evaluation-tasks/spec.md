# evaluation-tasks Specification

## Purpose

Defines how evaluation tasks are written, generated, validated and tagged, and which workloads the suite must cover, so that every trial result is trustworthy and traceable to a decision about envelope's defaults or its advantage over native shell tools.

## Requirements

### Requirement: Task definition
Each task SHALL be a directory containing: metadata (id, version, category, lanes, shells, tags, baseline eligibility, known-gap references, declared modifiable paths, agent timeout); a tool-neutral instruction; a fixture generator; a collect step; a judge; and an oracle. A task with missing parts or invalid metadata SHALL be rejected when the suite is loaded, with the reason. The suite SHALL have a content hash recorded with every job.

#### Scenario: Incomplete task rejected
- **WHEN** the suite is loaded and a task directory has no judge
- **THEN** loading reports that task as invalid with the missing part and the task is not scheduled

#### Scenario: Suite change is visible
- **WHEN** a task's instruction is edited between two jobs
- **THEN** the two jobs record different suite hashes and the changed task's new version

### Requirement: Host-side fixture generation
Fixtures SHALL be generated on the runner host by the task's generator from a per-trial seed, producing the fixture files (with exact bytes, including non-UTF-8 encodings) and an answer key. Only the fixture files SHALL be transferred to the target. The seed and answer key SHALL remain on the runner host. A task MAY declare per-job generation for fixtures that are expensive to create (for example database datasets); the answer key is then per job.

#### Scenario: Encoded fixture reaches the target unchanged
- **WHEN** a generator writes a CP1251 log and a UTF-16LE log for a Windows trial
- **THEN** the files on the target are byte-identical to the generated files

#### Scenario: Seed stays on the host
- **WHEN** a trial runs on the Windows VM
- **THEN** neither the seed nor the answer key is written to the VM

### Requirement: Answer-key confinement
During the agent phase, no answer key, seed or judge input SHALL be readable from the agent's environment (workspace, home directory, environment variables, process command lines, other trials' directories). Expected values SHALL require executing commands against the fixture to obtain. The suite SHALL include a check that searches a trial's environment for its answer key and seed.

#### Scenario: Answer from arithmetic does not pass
- **WHEN** a model answers a task without calling any tool
- **THEN** the answer does not match the per-trial expected value and the trial fails

#### Scenario: Confinement check
- **WHEN** the confinement check searches a running trial's target environment for the trial's answer key and seed
- **THEN** it finds neither

### Requirement: Instruction wording
Instructions for baseline-eligible tasks SHALL NOT name envelope, any specific tool or anything that distinguishes arms, so the same text is used for every arm; a linter SHALL reject violations when the suite is loaded. Literal commands, scripts, patterns and data SHALL appear in fenced blocks or fixture files, never inside quoted prose. On lanes where the native shell differs from the task's shell, only tasks that state a goal without literal shell code SHALL be baseline-eligible. Tasks exercising envelope-specific mechanics SHALL be marked ineligible for the baseline and MAY name envelope's tools.

#### Scenario: Same instruction for every arm
- **WHEN** a baseline-eligible task runs in an envelope arm and in the native arm
- **THEN** both trials receive byte-identical instruction text

#### Scenario: Leaking instruction rejected
- **WHEN** a baseline-eligible task's instruction mentions `run` or `output` as tool names
- **THEN** suite loading rejects the task with the offending text

#### Scenario: Bash literal on Windows
- **WHEN** a Windows task contains a fenced bash command and is marked baseline-eligible
- **THEN** suite loading rejects the eligibility

### Requirement: Collect and judge verdict
A task's collect step SHALL run on the target after the agent phase and emit observations (file hashes, requested facts, file contents the judge needs) as JSON. Its judge SHALL run on the runner host and produce a verdict (pass or fail with a reason) from the observations, the agent's final answer and the answer key. Judges SHALL NOT depend on which commands the agent ran, except for tasks marked as envelope mechanics, which MAY inspect envelope's audit log. A crash of the generator, upload, collect or judge SHALL produce an `infra` result, not a model failure.

#### Scenario: State-based pass
- **WHEN** a file-wrangling task's target files end in the required state, regardless of the commands used
- **THEN** the judge passes the trial

#### Scenario: Collect crash
- **WHEN** collect fails on the target with an encoding error
- **THEN** the trial is recorded as `infra` with the error and excluded from comparisons

### Requirement: Workspace integrity
The generator SHALL record a manifest of fixture files. Tasks SHALL declare which fixture paths they may modify. The judge SHALL fail the trial if a fixture file outside the declared paths was modified or deleted, naming it. Files created by the agent SHALL NOT fail the trial; they SHALL be reported as an invariant. Version-control internals (`.git/`) SHALL be excluded from the manifest by default; tasks that need them SHALL check repository state explicitly (refs, HEAD, object integrity). Every workspace SHALL provide a `scratch/` directory for temporary files.

#### Scenario: Collateral damage fails
- **WHEN** a bulk-rename task is solved but an unrelated fixture file was deleted
- **THEN** the trial fails with a reason listing the deleted file

#### Scenario: Read-only git task
- **WHEN** a model runs `git status` and `git log` in a read-only git task
- **THEN** the index refresh does not fail the trial

### Requirement: Oracle validation
Every task SHALL have an oracle that solves it without a model. Before model trials start on a target, each scheduled task's oracle SHALL run there; a task whose oracle does not pass its own judge SHALL be excluded from that target's run and reported as broken with the judge's reason.

#### Scenario: Broken task on one lane
- **WHEN** a task's oracle passes on the Linux lane but fails on the Windows lane
- **THEN** the task runs model trials only on the Linux lane and the report lists it as broken on Windows

### Requirement: Lever and claim tags
Each task SHALL declare at least one tag naming a lever (a tunable envelope default or wording: output budget, head/tail split, tool descriptions, hint wording, argument coercion) or a claim (a behaviour envelope asserts over native shell tools: output recovery, long-running commands, the user's shell, encodings, lockdown hints, process cleanup). Canary tasks SHALL carry the `canary` tag. A task without tags SHALL be rejected when the suite is loaded.

#### Scenario: Untagged task rejected
- **WHEN** a task declares no tags
- **THEN** suite loading reports it as invalid

### Requirement: Known-gap tasks
A task that exercises a known, not-yet-fixed envelope gap SHALL reference the gap by id in its metadata. The gaps known at the start of this change are `gitbash-no-codepage-fallback` (any non-UTF-8 output in git-bash, alone or mixed with UTF-8, is decoded as UTF-8) and `git-editor-default` (no `GIT_EDITOR` default, so editor-opening git commands hang). Failures of known-gap tasks SHALL be reported separately from other results and still counted, so a later fix is measured against the recorded baseline.

#### Scenario: Known gap reported separately
- **WHEN** a task reading CP866 output from a native Windows tool in git-bash fails
- **THEN** the report lists the failure under `gitbash-no-codepage-fallback` with its pass rate, not as a regression

### Requirement: Suite coverage
The suite SHALL contain, for every lane, at least one canary task and tasks for large output, long-running commands, errors and hints, heavy scripts, and cwd/env parameters. The Windows lane SHALL additionally cover, with git-bash as the primary shell: git (history search, large logs, a merge that would open an editor), log digging (multi-file tracing, time windows, stack traces, UTF-16LE and CP1251 logs, rotated compressed logs), file wrangling (bulk rename with spaces and Cyrillic names, duplicates, CSV transformation, CP1251 to UTF-8 conversion, long paths), scripts and grep pipelines, MongoDB and Redis command-line clients against remote servers (large result sets, authentication, arguments starting with `/`, a client started without arguments), CP866 output from native Windows tools alone and mixed with UTF-8 output, CRLF in pipelines, an executable blocked by application control surfacing as exit 126, and PowerShell and cmd edge cases (Constrained Language Mode, cmd's command-line limit, shell choice). A coverage report SHALL list every required area per lane with its tasks and flag areas without one.

#### Scenario: Coverage check
- **WHEN** the suite is loaded for the Windows lane
- **THEN** the coverage report lists every required area with the tasks that cover it and flags any area without a task
