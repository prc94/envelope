# Spec Delta

## MODIFIED Requirements

### Requirement: Tool catalog
Envelope SHALL expose exactly the tools `run`, `output`, `stop` and `processes`, listed in a deterministic order. Each tool SHALL have a flat input schema (no nested object parameters are required) and a description that is self-sufficient for a model that sees only the tool definition. The `run` description SHALL include the words "shell", "command" and "terminal" and the names of the shells available, and SHALL state that commands still running when the wait ends continue in the background behind a handle. The `stop` and `processes` descriptions SHALL include the words "process" and "background". Tools SHALL carry annotations: `run` as not read-only, potentially destructive and open-world; `stop` as not read-only and destructive; `output` and `processes` as read-only. For `2026-07-28` clients, `tools/list` results SHALL include `ttlMs` and `cacheScope`.

#### Scenario: Deterministic listing
- **WHEN** a client calls `tools/list` twice in the same server lifetime
- **THEN** both responses list the same tools in the same order with identical definitions

#### Scenario: Tool found by search
- **WHEN** a client that defers tool loading searches tool descriptions for "terminal" or "powershell"
- **THEN** the `run` tool description matches

#### Scenario: Process tools found by search
- **WHEN** a client that defers tool loading searches tool descriptions for "background process"
- **THEN** the `processes` and `stop` tool descriptions match

### Requirement: Cancellation
When envelope receives `notifications/cancelled` for an in-flight `tools/call`, it SHALL NOT send a response for the cancelled request. Cancelling a `run` call SHALL terminate the entire process tree started by that call; a `run` call that started nothing because a process of the same name was running SHALL only end its wait, and a cancelled `restart` SHALL still complete stopping the old process and SHALL terminate the new one if it was started. Cancelling an `output` or `processes` call SHALL only end its wait and SHALL NOT affect any process. A cancelled `stop` call SHALL still complete the termination it started.

#### Scenario: User aborts a running command
- **WHEN** a client cancels a `run` call whose command spawned child and grandchild processes
- **THEN** all of those processes are terminated and no result is sent for that request

#### Scenario: Cancelled idempotent start
- **WHEN** a client cancels a `run` call with `name: "web"` that is waiting on the already running process named `web`
- **THEN** no result is sent for that request and `web` keeps running

#### Scenario: Cancelled wait leaves the process alone
- **WHEN** a client cancels an `output` call that is waiting for a handed-back server to print a line
- **THEN** no result is sent for that request and the server keeps running

### Requirement: Generated server instructions
Envelope SHALL provide server instructions generated at startup describing: the operating system, the detected shells with versions and the default shell, notable restrictions (PowerShell language mode, effective execution policy), git-bash usage notes when git-bash is present, how to retrieve truncated output, and that commands still running after the wait continue in the background and can be waited on, listed and stopped. Instructions SHALL be at most 2,000 characters.

#### Scenario: Windows with git-bash and Constrained Language Mode
- **WHEN** envelope starts on Windows where git-bash and pwsh are installed and PowerShell runs in Constrained Language Mode
- **THEN** the instructions name git-bash as default, list pwsh, and state that PowerShell runs in Constrained Language Mode

#### Scenario: Background use explained
- **WHEN** a client reads the server instructions
- **THEN** they state that a long-running command returns a handle and name the `output`, `processes` and `stop` tools

### Requirement: Tool error semantics
Envelope SHALL set `isError: true` only when envelope itself could not perform the requested operation (invalid or uncoercible arguments, unknown shell, unknown or expired handle or process name, malformed wait condition, live-process cap reached without an evictable process, policy denial, spawn failure). A command that ran and exited with a non-zero status, a command still running when its wait ended, and a wait whose condition was not met SHALL be returned as normal results. Protocol-level failures (unknown method, malformed request) SHALL be returned as JSON-RPC errors.

#### Scenario: Failing command is not a tool error
- **WHEN** `run` executes `exit 3`
- **THEN** the result has `isError` absent or false and reports exit code 3

#### Scenario: Unknown shell is a tool error
- **WHEN** `run` is called with `shell: "tcsh"` and tcsh is not available
- **THEN** the result has `isError: true` and lists the available shells

#### Scenario: Unmet wait is not a tool error
- **WHEN** `output` is called with `wait_for: "regex:ready"` and `wait_s: 5` and no such line appears
- **THEN** the result has `isError` absent or false and states that the wait expired
