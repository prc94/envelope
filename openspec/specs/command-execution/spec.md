# Command Execution Specification

## Purpose

Runs a single command to completion in a selected shell with explicit working directory, input, environment and time limits, and guarantees that every process it starts is cleaned up, including after cancellation, timeout or an envelope crash.

## Requirements

### Requirement: The run tool
The `run` tool SHALL accept `command` (required, string) and optional `shell`, `cwd`, `timeout_s`, `stdin` and `env`. Each call SHALL start a new shell process; no working directory, environment or shell state SHALL carry over between calls. The call SHALL return when the command's process tree has exited or been terminated.

#### Scenario: State does not persist
- **WHEN** one `run` call executes `cd /tmp && export X=1` and a second `run` call executes `pwd; echo $X`
- **THEN** the second call prints the envelope default working directory and an empty value for X

### Requirement: Working directory
`cwd` SHALL default to envelope's configured default working directory (by default the directory envelope was started in). `cwd` SHALL be accepted in the host's native form and, for git-bash, also in MSYS form (`/c/Users/x`) or with forward slashes (`C:/Users/x`), and SHALL be translated to the form the target shell expects. A `cwd` that does not exist or is not a directory SHALL produce a tool error naming the path.

#### Scenario: Windows path for git-bash
- **WHEN** `run` executes `pwd` in git-bash with `cwd: "C:\\src\\app"`
- **THEN** the command runs in that directory and prints `/c/src/app`

#### Scenario: MSYS path for pwsh
- **WHEN** `run` executes `Get-Location` in pwsh with `cwd: "/c/src/app"` on Windows
- **THEN** the command runs in `C:\src\app`

#### Scenario: Missing directory
- **WHEN** `run` is called with `cwd` pointing to a non-existent directory
- **THEN** envelope returns a tool error stating the directory does not exist

### Requirement: Standard input
The command's standard input SHALL be closed (end-of-file) by default so that commands waiting for input fail fast instead of hanging. When `stdin` is provided, its text SHALL be written to the command's standard input followed by end-of-file.

#### Scenario: Prompting command does not hang
- **WHEN** `run` executes `read -p "Continue? " x; echo "got:$x"` without `stdin`
- **THEN** the command finishes immediately and prints `got:`

#### Scenario: Provided input
- **WHEN** `run` executes `wc -l` with `stdin: "a\nb\nc\n"`
- **THEN** the output is `3`

### Requirement: Non-interactive environment defaults
Envelope SHALL set environment defaults that prevent interactive prompts, pagers and colored output, including at least: `NO_COLOR=1`, `TERM=dumb`, `PAGER=cat`, `GIT_PAGER=cat`, `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `DEBIAN_FRONTEND=noninteractive`, `PYTHONUNBUFFERED=1` and `PYTHONIOENCODING=utf-8`. The defaults SHALL be configurable, and values in the `env` argument SHALL take precedence over them.

#### Scenario: Git does not open a pager
- **WHEN** `run` executes `git log` in a repository with many commits
- **THEN** the command completes without waiting on a pager

#### Scenario: Explicit override
- **WHEN** `run` is called with `env: {"TERM": "xterm"}`
- **THEN** the command sees `TERM=xterm`

### Requirement: Environment scrubbing
Envelope SHALL remove inherited environment variables whose names match configured removal patterns before starting commands. The default patterns SHALL include `*_API_KEY`, `*_TOKEN`, `*_SECRET` and `OPENROUTER_*`. Variables passed explicitly via `env` SHALL NOT be removed.

#### Scenario: Harness secret is not visible
- **WHEN** envelope was started with `OPENROUTER_API_KEY` in its environment and `run` executes `echo "[$OPENROUTER_API_KEY]"`
- **THEN** the output is `[]`

### Requirement: Timeouts
`timeout_s` SHALL default to 120 seconds and SHALL be clamped to a configurable maximum (default 3,600 seconds). When the timeout elapses envelope SHALL terminate the command's entire process tree and return the output captured so far, marked as timed out, with the elapsed time.

#### Scenario: Command exceeds timeout
- **WHEN** `run` executes `sleep 30` with `timeout_s: 2`
- **THEN** the call returns after about 2 seconds, the result is marked timed out, and no `sleep` process remains

#### Scenario: Timeout above maximum
- **WHEN** `run` is called with `timeout_s: 999999` and the configured maximum is 3,600
- **THEN** the command runs with a 3,600-second timeout and the result notes the clamping

### Requirement: Process tree termination
When envelope terminates a command (timeout, cancellation or shutdown) it SHALL terminate every process started by that command, including descendants that were re-parented or detached from the shell's process group, excluding only processes that explicitly escaped via OS-level daemonization outside envelope's containment on POSIX (which SHALL be documented as a limitation).

#### Scenario: Windows grandchild processes
- **WHEN** a git-bash command starts `node server.js` which starts a child process, and the call is cancelled
- **THEN** git-bash, node and node's child are all terminated

#### Scenario: Background job inside a command on Linux
- **WHEN** `run` executes `sleep 300 & sleep 300` with `timeout_s: 1`
- **THEN** both sleep processes are terminated when the timeout fires

### Requirement: Crash-safe cleanup
If the envelope process exits for any reason, including being killed, processes started by envelope SHALL be terminated by the operating system on Windows and Linux. On macOS envelope SHALL provide equivalent cleanup through a supervising mechanism, or document the residual risk if the P0 spike shows no reliable mechanism.

#### Scenario: Envelope is force-killed on Windows
- **WHEN** envelope is terminated with `taskkill /F` while a `run` call is executing a long-running server process
- **THEN** that server process is terminated by the operating system

#### Scenario: Envelope is SIGKILLed on Linux
- **WHEN** envelope receives SIGKILL while a `run` call executes `sleep 1000`
- **THEN** the sleep process is terminated

### Requirement: Concurrency limit
Envelope SHALL run at most a configurable number of `run` calls concurrently (default 8). Calls beyond the limit SHALL wait for a free slot, and waiting time SHALL count toward their timeout.

#### Scenario: Excess parallel calls
- **WHEN** a client issues 10 concurrent `run` calls with the default limit
- **THEN** at most 8 commands execute at once and all 10 calls eventually return

### Requirement: Spawn failures
If the shell process cannot be started (missing executable, permission denied, blocked by application control policy), envelope SHALL return a tool error that states the shell, the path and the operating-system error, and SHALL identify application-control blocks explicitly.

#### Scenario: Shell blocked by policy
- **WHEN** starting the selected shell fails with Windows error 1260 or 4551
- **THEN** the tool error states that the shell executable is blocked by group policy / application control
