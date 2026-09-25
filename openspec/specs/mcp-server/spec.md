# MCP Server Specification

## Purpose

Exposes envelope to MCP clients over stdio: protocol version negotiation for current and previous MCP revisions, tool discovery and invocation, progress, cancellation, server instructions and argument handling tuned for open-weight models.

## Requirements

### Requirement: Single-file distribution without native code
Envelope SHALL be distributed as a single file that runs with a stock CPython 3.12 or newer interpreter (`python envelope.pyz`) on Linux, macOS and Windows, with no installation step, no third-party runtime dependencies and no native code of its own.

#### Scenario: Run on a locked-down Windows machine
- **WHEN** a user on Windows with AppLocker blocking user-space executables runs `python envelope.pyz` with an approved Python 3.12 installation
- **THEN** the server starts and serves MCP over stdio without loading any file other than the interpreter's own standard library and `envelope.pyz`

#### Scenario: Unsupported interpreter
- **WHEN** envelope is started with Python older than 3.12
- **THEN** it exits with a non-zero status and a message on stderr naming the minimum supported version

### Requirement: Stdio transport
Envelope SHALL serve MCP over stdio as newline-delimited JSON-RPC 2.0 messages on stdin/stdout, SHALL write nothing but protocol messages to stdout, and SHALL write diagnostics only to stderr.

#### Scenario: Diagnostic output does not corrupt the protocol stream
- **WHEN** envelope logs a warning while serving requests
- **THEN** the warning appears on stderr and stdout contains only valid JSON-RPC messages

#### Scenario: Client closes stdin
- **WHEN** the client closes envelope's stdin
- **THEN** envelope terminates all child processes it started and exits

### Requirement: Dual protocol version support
Envelope SHALL support MCP protocol revisions `2026-07-28` and `2025-11-25`. For `2025-11-25` clients it SHALL implement the `initialize` / `notifications/initialized` handshake and `ping`. For `2026-07-28` clients it SHALL implement `server/discover`, accept the protocol version and client capabilities carried in each request's `_meta`, include its server identity in each result's `_meta`, and include `resultType: "complete"` on results. Requests declaring an unsupported protocol version SHALL be rejected with the `UnsupportedProtocolVersion` error listing the supported versions.

#### Scenario: Legacy client handshake
- **WHEN** a client sends `initialize` with `protocolVersion: "2025-11-25"`
- **THEN** envelope responds with protocol version `2025-11-25`, its server info, its capabilities and its instructions, and then serves `tools/list` and `tools/call`

#### Scenario: Stateless client without handshake
- **WHEN** a client sends `tools/call` whose `_meta` declares protocol version `2026-07-28`, without any prior `initialize`
- **THEN** envelope processes the call and returns a result with `resultType: "complete"` and envelope's server info in `_meta`

#### Scenario: Discovery
- **WHEN** a client sends `server/discover`
- **THEN** envelope returns its supported protocol versions, capabilities, server identity and instructions

#### Scenario: Unsupported version
- **WHEN** a request declares protocol version `2023-01-01`
- **THEN** envelope returns the `UnsupportedProtocolVersion` error and lists `2026-07-28` and `2025-11-25` as supported

### Requirement: Tool catalog
Envelope SHALL expose exactly the tools `run` and `output` in this change, listed in a deterministic order. Each tool SHALL have a flat input schema (no nested object parameters are required) and a description that is self-sufficient for a model that sees only the tool definition, including the words "shell", "command" and "terminal" and the names of the shells available. Tools SHALL carry annotations: `run` as not read-only, potentially destructive and open-world; `output` as read-only. For `2026-07-28` clients, `tools/list` results SHALL include `ttlMs` and `cacheScope`.

#### Scenario: Deterministic listing
- **WHEN** a client calls `tools/list` twice in the same server lifetime
- **THEN** both responses list the same tools in the same order with identical definitions

#### Scenario: Tool found by search
- **WHEN** a client that defers tool loading searches tool descriptions for "terminal" or "powershell"
- **THEN** the `run` tool description matches

### Requirement: Lenient argument coercion
Envelope SHALL accept tool arguments whose types deviate from the schema in ways common to open-weight models and coerce them: numeric strings to numbers, `"true"`/`"false"` to booleans, JSON-encoded strings to the object or array they encode, and environment variables given either as an object or as newline-separated `KEY=value` lines. Every coercion SHALL be recorded in the audit log. Arguments that cannot be coerced SHALL produce a tool error naming the parameter, the received value type and the expected form.

#### Scenario: Stringified number
- **WHEN** `run` is called with `timeout_s: "30"`
- **THEN** the command runs with a 30-second timeout and the audit log records a coercion for `timeout_s`

#### Scenario: Stringified object
- **WHEN** `run` is called with `env: "{\"FOO\":\"1\"}"`
- **THEN** the command runs with `FOO=1` in its environment

#### Scenario: Uncoercible argument
- **WHEN** `run` is called with `timeout_s: "soon"`
- **THEN** envelope returns a tool error stating that `timeout_s` must be a number of seconds

### Requirement: Progress notifications
While a `tools/call` is executing and the request carried a progress token, envelope SHALL send `notifications/progress` at least every 5 seconds, each with a monotonically increasing progress value and a short message describing elapsed time, output size so far and the last output line (truncated).

#### Scenario: Long build keeps client timeout alive
- **WHEN** a client with a 30-second request timeout that resets on progress calls `run` with a command taking 90 seconds and supplies a progress token
- **THEN** envelope emits progress notifications at intervals of at most 5 seconds and the call completes with the command's result

#### Scenario: No progress token
- **WHEN** a `tools/call` request carries no progress token
- **THEN** envelope sends no progress notifications for it

### Requirement: Cancellation
When envelope receives `notifications/cancelled` for an in-flight `tools/call`, it SHALL terminate the entire process tree started for that call and SHALL NOT send a response for the cancelled request.

#### Scenario: User aborts a running command
- **WHEN** a client cancels a `run` call whose command spawned child and grandchild processes
- **THEN** all of those processes are terminated and no result is sent for that request

### Requirement: Generated server instructions
Envelope SHALL provide server instructions generated at startup describing: the operating system, the detected shells with versions and the default shell, notable restrictions (PowerShell language mode, effective execution policy), git-bash usage notes when git-bash is present, and how to retrieve truncated output. Instructions SHALL be at most 2,000 characters.

#### Scenario: Windows with git-bash and Constrained Language Mode
- **WHEN** envelope starts on Windows where git-bash and pwsh are installed and PowerShell runs in Constrained Language Mode
- **THEN** the instructions name git-bash as default, list pwsh, and state that PowerShell runs in Constrained Language Mode

### Requirement: Tool error semantics
Envelope SHALL set `isError: true` only when envelope itself could not perform the requested operation (invalid or uncoercible arguments, unknown shell, unknown or expired handle, policy denial, spawn failure). A command that ran and exited with a non-zero status SHALL be returned as a normal result. Protocol-level failures (unknown method, malformed request) SHALL be returned as JSON-RPC errors.

#### Scenario: Failing command is not a tool error
- **WHEN** `run` executes `exit 3`
- **THEN** the result has `isError` absent or false and reports exit code 3

#### Scenario: Unknown shell is a tool error
- **WHEN** `run` is called with `shell: "tcsh"` and tcsh is not available
- **THEN** the result has `isError: true` and lists the available shells
