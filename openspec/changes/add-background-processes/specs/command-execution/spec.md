# Spec Delta

## MODIFIED Requirements

### Requirement: The run tool
The `run` tool SHALL accept `command` (required, string) and optional `shell`, `cwd`, `timeout_s`, `stdin`, `env`, `wait_s`, `wait_for`, `name` and `restart`. Each call SHALL start a new shell process, except a call naming a process that is already running (see the background-processes capability); no working directory, environment or shell state SHALL carry over between calls. The call SHALL return when the command has finished, or when its wait ends while the command keeps running, in which case the result identifies the running process by its handle. A command is finished when its shell has exited and its output streams are closed.

#### Scenario: State does not persist
- **WHEN** one `run` call executes `cd /tmp && export X=1` and a second `run` call executes `pwd; echo $X`
- **THEN** the second call prints the envelope default working directory and an empty value for X

#### Scenario: Command outlives the call
- **WHEN** `run` executes `sleep 60; echo done` with `wait_s: 5`
- **THEN** the call returns after about 5 seconds with a running result and a handle, and `done` appears in that handle's output about 55 seconds later

### Requirement: Timeouts
`timeout_s` SHALL be a hard deadline, measured from the command's start and applying also after the command was handed back, after which envelope terminates the command's entire process tree whether or not the call that started it is still waiting. `timeout_s` SHALL have no default unless one is configured, and SHALL be clamped to a configurable maximum (default 3,600 seconds). When the timeout elapses during a call, the call SHALL return the output captured so far, marked as timed out, with the elapsed time; when it elapses after the process was handed back, the next result SHALL carry a completion notice stating that it timed out.

#### Scenario: Command exceeds timeout
- **WHEN** `run` executes `sleep 30` with `timeout_s: 2`
- **THEN** the call returns after about 2 seconds, the result is marked timed out, and no `sleep` process remains

#### Scenario: Timeout above maximum
- **WHEN** `run` is called with `timeout_s: 999999` and the configured maximum is 3,600
- **THEN** the command runs with a 3,600-second timeout and the result notes the clamping

#### Scenario: No timeout by default
- **WHEN** `run` executes `sleep 200` with `wait_s: 1` and no `timeout_s`, and no default timeout is configured
- **THEN** the call returns after about 1 second with a running result and the sleep is still running 150 seconds later

#### Scenario: Configured default restores a deadline
- **WHEN** the configuration sets `default_timeout_s = 120` and `run` executes `sleep 300` without `timeout_s`
- **THEN** the process tree is terminated about 120 seconds after it started

### Requirement: Process tree termination
When envelope terminates a command (timeout, cancellation, `stop`, eviction, maximum lifetime or shutdown) it SHALL terminate every process started by that command, including descendants that were re-parented or detached from the shell's process group, excluding only processes that explicitly escaped via OS-level daemonization outside envelope's containment on POSIX (which SHALL be documented as a limitation). When a command finishes (its shell exited and its output streams closed) while processes it started are still running, envelope SHALL terminate those processes and SHALL say so in the result that reports the command's final status, or in its completion notice when it had been handed back; this report SHALL be omitted when the command was being stopped.

#### Scenario: Windows grandchild processes
- **WHEN** a git-bash command starts `node server.js` which starts a child process, and the call is cancelled
- **THEN** git-bash, node and node's child are all terminated

#### Scenario: Background job inside a command on Linux
- **WHEN** `run` executes `sleep 300 & sleep 300` with `timeout_s: 1`
- **THEN** both sleep processes are terminated when the timeout fires

#### Scenario: Backgrounded server with redirected output
- **WHEN** `run` executes `nohup python -m http.server 8000 > server.log 2>&1 &` and the shell exits immediately
- **THEN** the server process is terminated, and the result states that a background process was terminated when its command finished and that running the server as its own command keeps it running

### Requirement: Crash-safe cleanup
If the envelope process exits for any reason, including being killed, processes started by envelope, including processes that were handed back and are no longer waited on by any call, SHALL be terminated by the operating system on Windows and Linux. On macOS envelope SHALL provide equivalent cleanup through a supervising mechanism, or document the residual risk if the P0 spike shows no reliable mechanism.

#### Scenario: Envelope is force-killed on Windows
- **WHEN** envelope is terminated with `taskkill /F` while a `run` call is executing a long-running server process
- **THEN** that server process is terminated by the operating system

#### Scenario: Envelope is SIGKILLed on Linux
- **WHEN** envelope receives SIGKILL while a `run` call executes `sleep 1000`
- **THEN** the sleep process is terminated

#### Scenario: Envelope is killed after hand-back
- **WHEN** a server was handed back an hour ago, no call is waiting on it, and envelope receives SIGKILL (Linux) or is terminated with `taskkill /F` (Windows)
- **THEN** the server's whole process tree is terminated

## ADDED Requirements

### Requirement: No terminal input
Commands SHALL NOT be able to block waiting for input from a terminal or console that nobody can type into. On POSIX, commands SHALL run without a controlling terminal, so that programs opening the terminal for a prompt fail immediately. On Windows, envelope SHALL start commands so that console-input reads fail where that is possible without breaking UTF-8 console output; otherwise a process waiting on console input SHALL be reported through the console-prompt hint where envelope can read the console text, and through the no-output-yet hint otherwise.

#### Scenario: Password prompt on Linux
- **WHEN** `run` executes a program that opens `/dev/tty` to ask for a password
- **THEN** the program fails immediately with an error about the missing terminal instead of hanging

#### Scenario: Console prompt on Windows
- **WHEN** `run` executes a Windows program that reads a confirmation from the console, without `stdin`
- **THEN** the program either fails immediately, or a result about the handed-back process carries the console-prompt hint quoting the confirmation text or, where the console text cannot be read, the no-output-yet hint

## REMOVED Requirements

### Requirement: Concurrency limit
**Reason**: With commands that keep running behind handles, a limit on concurrently executing calls that makes excess calls wait for a slot would block new calls behind long-lived servers indefinitely.
**Migration**: Replaced by the live-process cap of the background-processes capability (`max_running`, default 16, with least-recently-used eviction of unnamed processes or refusal). The configuration key `max_concurrency` is accepted as a deprecated alias of `max_running`.
