## Why

Coding agents driven by open models (Qwen, DeepSeek, GLM) need a shell tool that works equally well on Linux, macOS and Windows, including Windows without WSL and locked-down corporate machines where user-space binaries are blocked. Existing MCP shell servers either are abandoned (win-cli-mcp-server), depend on runtimes or native code that corp policy blocks, truncate output with no way to recover it, or ship string blocklists as "security". Envelope is a compact, zero-dependency MCP server that runs commands in bash, git-bash, PowerShell, cmd and other shells and returns rich, bounded, model-friendly results, with a policy seam ready for a later security classifier.

This change delivers the foundation: risk-reducing spikes (P0) and the core server (P1). Later phases (evaluation harness, background/interactive processes, security classifier) build on it and are proposed as separate changes.

## What Changes

- New Python (>=3.12, standard library only) MCP server distributed as a single zipapp (`envelope.pyz`), runnable as `python envelope.pyz` with no install step and no native code.
- Own stdio MCP protocol layer that serves protocol `2026-07-28` (stateless, `server/discover`, `_meta`-carried versions) and `2025-11-25` (initialize handshake) clients.
- Two tools: `run` (execute a command in a chosen shell with cwd, timeout, stdin, env) and `output` (page, tail or grep the full spooled output of a previous run by handle).
- Shell registry: detection of bash, sh, zsh, fish, git-bash, pwsh 7, Windows PowerShell 5.1 and cmd, plus user-defined custom shells; per-shell launch strategies including large-script delivery that works under AppLocker, Constrained Language Mode and GPO ExecutionPolicy.
- Execution engine: one process per call, closed stdin by default, non-interactive environment defaults, secret-scrubbing of inherited environment, timeouts, progress heartbeats, cancellation, and whole-process-tree termination that survives an envelope crash (Job Objects on Windows, process groups plus parent-death signalling on POSIX).
- Output pipeline: encoding normalization (UTF-8, UTF-16, OEM/ANSI code pages), ANSI and progress-bar cleanup, repeated-line folding, binary detection, full spooling to disk, and head+tail truncation within a budget sized for OpenCode and Qwen Code, with explicit recovery instructions.
- Result rendering: compact stable text header plus body for the model; lean metadata-only `structuredContent`; deterministic "within competence" hints (command not found, signals, crash codes, group-policy blocks, Constrained Language Mode, ExecutionPolicy refusals, timeouts).
- Execution policy seam: every exec request is normalized and passed through a `Decide()` step returning allow/ask/deny with reasons; P1 ships an allow-all policy plus simple limits (allowed shells, cwd roots, max command length). The request model already covers stdin input to running processes for P3.
- Configuration via TOML file, CLI flags and environment variables; JSONL audit log of every request and decision.
- `envelope doctor` diagnostic command reporting detected shells, PowerShell language mode and execution policy, encodings and restrictions.
- CI across Linux, Windows and macOS on Python 3.12, 3.13 and 3.14, including MCP interoperability checks against OpenCode and Qwen Code clients.

Out of scope for this change (roadmap, see design.md): evaluation harness (P2), background processes, stdin interaction, wait conditions and stop/restart (P3), security classifier (P4), Streamable HTTP transport, MCP Tasks extension, PTY/ConPTY. Explicit non-goals: file read/write/edit tools, SSH/remote execution, sandboxing or containment.

## Capabilities

### New Capabilities
- `mcp-server`: MCP protocol layer over stdio — dual protocol version support, discovery, tool listing and invocation, tool annotations, progress notifications, cancellation, generated server instructions, lenient argument coercion, error semantics.
- `shell-registry`: Detection and description of available shells per OS, default shell selection, custom shell definitions, per-shell launch strategy (argument vs. large-script delivery), git-bash and PowerShell specifics.
- `command-execution`: Running a command in a selected shell — cwd handling and path translation, stdin, environment defaults and scrubbing, timeouts, concurrency limit, process-tree termination and crash-safe cleanup.
- `output-pipeline`: Capturing, decoding, cleaning, spooling and truncating process output; the `output` tool for paging, tailing and grepping spooled output by handle; retention.
- `result-rendering`: The model-facing result format — text header and body, truncation markers, `structuredContent` metadata, `isError` semantics, and the deterministic hint catalog.
- `execution-policy`: The policy decision seam — normalized request model (exec and input), decision outcomes with reasons, P1 allow-all policy with basic limits, and how deny/ask surface to the model.
- `configuration`: Configuration sources and precedence (defaults, user TOML file, environment variables, CLI flags), validation, audit logging and diagnostic logging.
- `diagnostics`: The `envelope doctor` command reporting environment detection and restrictions.

### Modified Capabilities
<!-- None: greenfield project, no existing specs. -->

## Impact

- New codebase (greenfield): Python package `envelope` built into `envelope.pyz`; no runtime dependencies; dev-only tooling (pytest, pyright, ruff).
- Target runtimes: CPython 3.12+ on Linux, macOS and Windows (x64/arm64); corp environments with AppLocker/WDAC, PowerShell Constrained Language Mode and GPO ExecutionPolicy.
- Target clients: OpenCode (TypeScript MCP SDK v1, protocol 2025-11-25) and Qwen Code (TypeScript MCP SDK v2), both driven by open models via OpenRouter; any spec-compliant MCP client.
- Host systems: spawns shell processes, writes spooled output and audit logs to per-user cache/state directories.
