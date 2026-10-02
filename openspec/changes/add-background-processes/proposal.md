# Proposal

## Why

Every `run` call today blocks until the command's whole process tree is gone, and anything the command leaves running is killed. The model therefore cannot start a dev server and then probe it, cannot keep a watcher running, and cannot follow a build that outlives one tool call. The harnesses also bound a call from outside: Qwen Code 0.24.5 fails any MCP call after 10 minutes regardless of progress, and OpenCode 1.18 fails a call at 60 s unless progress notifications keep resetting its timer. Both harnesses' own shell tools and every comparable agent tool (Codex unified exec, Claude Code, Qwen Code, Gemini CLI) now let a command keep running behind a handle. This is roadmap phase P3a, narrowed to long jobs and persistent processes; interactive programs (`input`, control keys, REPLs) follow in their own change.

## What Changes

- **Yield by default.** `run` blocks for at most `wait_s` (default 30 s, clamped to a configurable maximum). A command that finishes in time returns its result as today. A command still running returns a *running* result with a handle, the output so far and how to continue, and keeps running.
- **Wait conditions** on `run` and `output`: `wait_for` of `exit`, `regex:<pattern>`, `port:<n>` or `idle:<seconds>`, each bounded by `wait_s`. An expired wait is a normal result, not an error.
- **Incremental reads.** `output` on a handle with unread output returns only the lines added since the model's last read, and can wait for more.
- **Named processes.** `run(name=...)` is idempotent: while a process of that name runs, a second call returns its status instead of starting a copy; `restart: true` stops it and starts the new command.
- **New `stop` tool:** a termination request where the platform can deliver one (SIGTERM on POSIX; CTRL+BREAK on Windows only if experiments show it reaches programs through envelope's launch chain), a grace period, then a forced kill of the whole tree; returns the final status, whether the stop was graceful or forced, and the last output.
- **New `processes` tool:** lists envelope-started processes, running and recently finished, with state, uptime or exit status, name, listening ports and last line.
- **Completion notices:** when a handed-back process ends, the next result from any envelope tool notes it once.
- **BREAKING — timeout semantics:** `timeout_s` stays a hard kill deadline but no longer has a 120 s default; without it, a handed-back process runs until it exits, is stopped, is evicted, or envelope exits.
- **BREAKING — concurrency:** the limit of 8 concurrently *executing calls* that queue for a slot becomes a cap on *live processes* (`max_running`, default 16). Exceeding it stops the least-recently-used unnamed process (or, with `evict_policy = "refuse"`, fails the new command); calls no longer queue. `max_concurrency` becomes a deprecated alias.
- **New hints:** a background job started with `&`/`nohup`/`Start-Process` that was killed when its command finished; port already in use, naming the owning process; a process with no output yet (possible pipe buffering); on Windows, a process whose hidden console shows a prompt (if experiments show the console text can be read).
- **Live output of running processes:** unterminated lines (prompts, `\r` progress) become visible while the process runs; spools of running processes are protected from cleanup, including by other envelope instances.
- Every new limit and default is a configuration key.
- **Windows experiments** decide three things before they are built: whether CTRL+BREAK can be delivered for a graceful stop (otherwise Windows stops are forced), whether commands can start without console input so console prompts fail fast, and whether a hidden console's text can be read for the console-prompt hint.
- **Evaluation:** existing jobs migrated to the four-tool surface; a background-process task group; a native-shell arm with Qwen Code's own background tools allowed; metrics for handles, waits and stops; a predeclared non-inferiority gate on the existing screening slices before the change is accepted.

Out of scope: `input` / control keys / REPL interaction and its policy decisions; detached processes that survive envelope (P3b); multi-handle waits and per-line timestamps (P3b); MCP Tasks; PTY/ConPTY.

## Capabilities

### New Capabilities
- `background-processes`: Handed-back processes and their lifecycle — yielding, handles and names, wait conditions, incremental reads, restart, the `stop` and `processes` tools, completion notices, the live-process cap and eviction, and the quiet-process and console-prompt hints.

### Modified Capabilities
- `command-execution`: `run` may return while the command is still running; timeout loses its default; the concurrency limit becomes the live-process cap; tree termination covers `stop` and eviction.
- `output-pipeline`: spool of a running process is readable while it grows, including unterminated lines; `output` gains waits and unread-lines reading; spools of running processes are never evicted, by any envelope instance.
- `mcp-server`: tool catalog becomes `run`, `output`, `stop`, `processes`; cancellation semantics for waits, idempotent starts and restarts; server instructions and tool error semantics.
- `result-rendering`: a running-state header; process-state metadata; completion notices; new hints.
- `configuration`: new settings for waits, the live-process cap, eviction, lifetime, stop grace and listing size; `max_concurrency` deprecated with a defined precedence; audit records for lifecycle actions and `process_end` records for asynchronous endings.
- `execution-policy`: a restart's new command is decided before the old process is stopped; stopping, eviction, waits and joins are explicitly outside the decision but audited; the normalized request gains name, restart and an optional timeout.
- `evaluation-tasks`: suite coverage adds background processes; a `background-processes` claim tag.
- `evaluation-runner`: metrics for yielded runs, waits, stops and processes still running at trial end.

## Impact

- Code: `envelope/execution/` (runner split into start/supervise vs wait; process table; graceful stop per backend; Windows ctrl-break helper; port tables), `envelope/output/spool.py` (live reader, read cursor), `envelope/tools/` (`run`, `output`, new `stop` and `processes`), `envelope/render/` (running header, notices, hints), `envelope/config/settings.py`, `envelope/services.py`, `envelope/protocol/server.py` (catalog), `envelope/instructions.py`.
- Stdlib only, as before; Windows additions use `ctypes` (`kernel32` console functions, `iphlpapi.GetExtendedTcpTable`).
- Behaviour change for existing clients: a command running longer than 30 s now returns a handle instead of blocking; the P2 `interop-long` task (a 40 s command) exercises exactly this path.
- Eval: new tasks under `eval/tasks/`, job files, Qwen adapter tool-set options for the native arm; Windows VM experiments (console-less start, reading a hidden console's text, ctrl-break delivery through the launch chain and batch wrappers, port table as a standard user); every existing eval job and the oracle runner move to the four-tool set, and a regression gate compares the P2 and P3a builds on the existing screening slices.
