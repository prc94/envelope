# Design

## Context

See proposal.md for motivation. The relevant current state:

- `execution/runner.py:execute` owns a command from spawn to cleanup inside one tool call: pump threads feed an asyncio queue, the consumer writes to a `SpoolWriter`, and the call returns when the shell has exited **and** both pipes hit EOF, then `terminate_tree()` removes detached leftovers. Timeout, cancellation and cleanup are all expressed relative to the calling task. `spawn` runs in an `asyncio.to_thread` worker.
- `Services.limiter` is an `asyncio.Semaphore(max_concurrency=8)` held for the whole call.
- Spool: `SpoolManager.open()` refuses a handle whose `meta.complete` is false. `SpoolWriter` keeps a head segment plus 4 rolling tail segments and **unlinks** dropped segment files; frames carry timestamps. `StreamDecoder` holds bytes until a newline (or 1,024 bytes) before deciding the encoding, and `SpoolWriter._text` holds the partial line until a newline or `finish()`, so unterminated output is invisible until the process ends. `cleanup()`/`enforce_total()` protect only this instance's `_active` handles and use directory mtimes for retention.
- `OutputTool` renders in a worker thread (`asyncio.to_thread(self._view, ...)`, `tools/output.py:100`); `SpoolReader._starts` is computed once at construction.
- POSIX: every command runs under `_shim.py`, spawned with `start_new_session=True` (no controlling terminal); stdin is `DEVNULL` or a pipe closed after writing, so nothing can block on terminal or stdin input. SIGTERM to the shim means "kill the tree now" (it blocks SIGTERM until its wait loop). PDEATHSIG is armed by the shim for its parent thread's death. Windows: per-run nested Job Object, `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` — the child has a hidden console (the S1 `chcp 65001` mechanism depends on it), `TerminateJobObject`.
- The config loader already keeps per-source values (`loader.py:73-106`) before building `Settings`.
- Eval: every job's envelope arms declare `tool_set = ["mcp__envelope__run", "mcp__envelope__output"]` and the oracle runner hard-codes the same pair (`runner/oracle.py:16`); the tool-set check turns any extra exposed tool into `infra`.
- P2 data: of 1,626 shell calls across all P2 trials, median 0.2 s, p95 ~1 s; only 20 exceeded 30 s, almost all `interop-long` (a deliberate 40 s sleep). The current suite exercises no background work.

### Evidence gathered for this design (2026-09-30)

Checked independently by two adversarial reviews (a Claude agent and Codex/GPT-6 Astra); corrections from those reviews are folded in.

| Question | Finding | Source |
|---|---|---|
| Qwen Code 0.24.5 call limits | `callTool` with `timeout` = `mcpServers.<n>.timeout ?? 10 min`, **not** reset by progress; separate idle timer (default 300 s, `QWEN_CODE_MCP_TOOL_IDLE_TIMEOUT_MS`) reset by progress | `packages/core/src/tools/mcp-client.ts:94,1650`, `mcp-tool.ts:613-686`, `config/config.ts:3179-3183` at tag v0.24.5 |
| OpenCode 1.18.32 call limits | tool calls: `callTool(..., { timeout, resetTimeoutOnProgress: true, onprogress })` with `timeout = mcp.<n>.timeout ?? experimental.mcp_timeout`, otherwise the SDK's 60 s; the 30 s `DEFAULT_TIMEOUT` applies only to connecting and listing tools; its shell tool has no background mode and a 2-minute default | `packages/opencode/src/mcp/catalog.ts:11,54-65`, `mcp/index.ts:286,661-682`, `tool/shell.ts:347` at v1.18.32 |
| Qwen Code native background | `run_shell_command(is_background)`, output to a file + status file, `task_stop`, `monitor`; hint to use background after a long foreground run and "do NOT re-run"; warns that programs block-buffer on pipes | `packages/core/src/tools/shell.ts:128-133,1366-1384`, `tool-names.ts:47,57` at v0.24.5; QwenLM/qwen-code#3634 |
| Codex unified exec (main branch, not pinned) | `exec_command` (`yield_time_ms` default 10 s, clamp 250 ms-30 s, Windows floor 10 s) returns exit code or `session_id`; `write_stdin` (empty = poll 5-300 s); output as deltas; max 64 processes, LRU pruning preferring exited ones and protecting the 8 most recent; stdin writes re-checked against launch permissions | `codex-rs/core/src/unified_exec/mod.rs`, `process_manager.rs`, `stdin_approval.rs`, `tools/handlers/shell_spec.rs` |
| Claude Code | `run_in_background`; a foreground command reaching its timeout is **moved to the background** instead of killed (except `sleep`); `TaskStop`, `Monitor` | code.claude.com/docs/en/tools-reference |
| MCP direction | 2026-07-28: servers needing cross-call state "mint an explicit handle ... and have the model pass it back"; the Tasks extension is server-directed and driven by the client through `tasks/get`/`update`/`cancel` (that the model never holds the handle is our inference); our clients do not implement it | blog.modelcontextprotocol.io 2026-07-28 RC post; Qwen Code 0.24.5 source |
| Windows ctrl events | `GenerateConsoleCtrlEvent` reaches only processes sharing the caller's console; `CTRL_C` cannot target a group and is disabled in `CREATE_NEW_PROCESS_GROUP` groups, `CTRL_BREAK` always calls handlers; `AttachConsole` allows one console per process and fails with `ERROR_INVALID_PARAMETER` for a process that no longer exists. That attaching resets the caller's ctrl handlers comes from a secondary source only | learn.microsoft.com: GenerateConsoleCtrlEvent, process creation flags, AttachConsole; blog.codetitans.pl (secondary) |
| Runtimes on CTRL_BREAK | Node exits (SIGBREAK) unless it listens — most dev servers (Vite, Next) do not, so for them it is a hard exit; .NET maps it to SIGQUIT, which the generic host handles as shutdown; uvicorn handles SIGBREAK; plain Python has no handler | nodejs.org process docs; dotnet/runtime `PosixSignalRegistration.Windows.cs`, `ConsoleLifetime.netcoreapp.cs`; uvicorn `server.py` (read by the author; not re-checked by the reviewers) |
| Listening ports | Windows `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER, AF_INET/AF_INET6)` gives owning PIDs (standard-user behaviour unverified); Linux `/proc/net/tcp{,6}` + socket inodes in `/proc/<pid>/fd` | learn.microsoft.com GetExtendedTcpTable |

## Goals / Non-Goals

**Goals:**
- Commands can outlive the call without the model predicting durations, with every single call staying well inside both harnesses' limits.
- Process ownership moves from the calling task to a server-lifetime process table, without weakening P1's guarantees (permit before spawn, whole-tree cleanup, crash-safe cleanup).
- Everything the model sees stays text-first and self-sufficient (D4).

**Non-Goals:**
- Writing to a running process's stdin, control keys, PTY/ConPTY (the interactive change).
- Surviving an envelope restart (P3b detached processes).
- Pushing events to the model; notices only piggyback on results the model requested.
- The A/B/C tool-surface comparison that P2 (E1) handed to P3a: with `input` deferred, only surfaces with and without a separate listing tool remained, and the user chose four tools outright. The comparison moves to the interactive change, where `input` exists.

## Decisions

### B1. Yield by default
`run` waits up to `wait_s`, then hands back a handle; the command keeps running. *Alternatives:* explicit opt-in (`background: true`, Qwen style) — the model must predict duration, a wrong guess either blocks the call up to the timeout or kills a build; timeout-becomes-background (Claude Code style) — no wrong guess, but the model is blocked for the whole timeout and long timeouts collide with Qwen's absolute 10-minute limit. Yielding keeps each call short by construction and gives jobs and servers one code path. P2 data says the change is invisible for >98% of existing calls.

### B2. Wait defaults: 30 s default, 300 s maximum
30 s is Codex's maximum yield and leaves margin under OpenCode's 60 s (which our heartbeats reset anyway). The 300 s maximum stays under Qwen Code's absolute 10-minute call limit with a factor of two; Qwen's 300 s idle timer is reset by our 5-second heartbeats. A user-configured client timeout below the wait arrives as a cancellation and, for `run`, kills the command (mcp-server Cancellation); `doctor` and the README say so, and envelope logs a warning when `wait_max_s >= 600`. Both values are configuration keys and the default wait is an eval lever (`default-wait` tag, a 10 s arm).

**Harness check result (2026-09-30), confirming the source-derived table above: Qwen Code 0.24.4 (the pinned target is 0.24.5; only the patch version differs on this machine) and OpenCode 1.18.32, driven for real (no mocked MCP or protocol layer) against a throwaway MCP stub (`tools/call` → sleeps `duration` seconds, emitting `notifications/progress` every 5 s) and `eval/tests/fake_model.py`'s scripted OpenAI-compatible server standing in for the model — so the check exercises the harnesses' real MCP-client and model-client code paths with zero real model spend, not just a source read.**
  - Both harnesses attach `_meta.progressToken` on `tools/call`: confirmed directly from the stub's own request log (`progressToken=4 meta={'progressToken': 4}` for Qwen, `progressToken=2` for OpenCode).
  - A 90 s call with 5 s progress completed in both (`qwen`: exit 0, `duration_ms: 90561`; `opencode`: exit 0, reaching the final `"text":"done"` step) — the progress heartbeats were delivered and read back throughout, not just accepted at the start.
  - A 400 s call completed in both, with all 80 progress notifications (every 5 s for 400 s) delivered right up to completion before either harness's final response — `qwen`: exit 0, `duration_ms: 400585`; `opencode`: exit 0, reaching `"text":"done"` at the same elapsed time. Well under Qwen's absolute 10-minute call ceiling, as expected from a 300 s configured maximum wait.
  - OpenCode additionally sent `notifications/cancelled` immediately after the final result in both runs — harmless (the call had already completed), but worth noting for the mcp-server Cancellation semantics: a client may send a cancellation notification for an already-finished call, which envelope's cancellation handling must treat as a no-op rather than an error.

  **Decision:** B2's 30 s/300 s defaults stand as designed; no change needed. The harness's actual behavior matches the source-derived table exactly.

### B3. Process table owns processes; calls only wait
`runner.execute` splits into:
- `start(request, permit) -> ManagedProcess`: permit check, spawn, pump threads, consumer task, shell-wait thread, deadline timers. The consumer and completion futures are tasks owned by a `ProcessTable` in `Services`, not by the calling request task.
- `ManagedProcess.wait(condition, deadline) -> WaitOutcome`: the only thing a tool call awaits.

**All table state is mutated on the event loop only** (pump and wait threads hand over with `call_soon_threadsafe`, as today), so "atomic" means "no `await` between check and update". The two places that previously spanned an `await` are made atomic by reservation: a start reserves its name and its slot synchronously (B6, B8) before awaiting `to_thread(spawn)`, and releases them if the spawn fails.

`ManagedProcess` holds: handle, name, shell, cwd, command, started/ended times, state and end reason, `ShellExit`, the `SpoolWriter` (until finished), read position, last-use time, waiter count, stopping flag, final-status-seen flag. Completion = shell exit + both pipes EOF (unchanged); leftovers are then terminated (unchanged) and counted for the `&`/`nohup` hint unless the stopping flag is set. Envelope shutdown (stdin EOF) terminates every live process through the backend as today, writes `process_end` records with reason `shutdown`, flushes the audit log, then resolves waiters. PDEATHSIG is tied to the thread that spawned the shim; spawning stays on the default executor's persistent worker threads, which is documented at the spawn site and covered by a test that SIGKILLs envelope after hand-back. *Alternative:* keep one task per call and "detach" it on yield — rejected: cancellation and lifetime would stay entangled with request tasks.

### B4. Live spool: snapshots, pinned segments, unterminated lines, ownership markers
- **Reader safety.** Readers may run in worker threads (`output` renders off the loop), so the writer publishes an immutable snapshot (segment list with line and byte counts, dropped counts, current line total) after each consumer batch, and the live reader works only from a snapshot. Segment deletion is deferred: a segment dropped by rolling is removed from the next snapshot, but its files are unlinked only when no reader holds a snapshot that contains it (a per-segment pin count, touched on the loop via `call_soon_threadsafe` when a reader finishes). This also avoids failing unlinks of open files on Windows. A reader that still hits a missing file reports the range as dropped instead of raising.
- **Unterminated lines.** The decoder decides the encoding on the first newline, the first 1,024 bytes, or after 200 ms without new bytes, whichever comes first. The writer exposes the current partial line of each stream (after `\r` resolution) in the snapshot; renders show it as the last line marked unterminated; `regex:` waits and hint assessments see it. When the newline arrives the line is written to the index once, as today.
- **Cross-instance ownership.** Each live spool directory gets an `owner` file (envelope PID and process start time); the table touches the directory's mtime every 10 minutes while the process runs. `cleanup()` and `enforce_total()` in any instance skip directories whose owner PID is alive with a matching start time, and treat a stale owner as finished. Live spools count toward the total cap but are never evicted; eviction runs before a result is rendered, never evicting the handle being rendered (this also fixes today's case where `release()` → `enforce_total()` can evict the spool that just finished).
- **Read positions.** A line number per process; a result advances it to the last line it shows. The unread view walks retained segments from the position: if the position lies in a dropped range it states the dropped line range and continues at the first retained line, and its omission markers only ever name retained ranges (the head/tail renderer's single-gap assumption, `render.py:133-134`, does not hold for unread ranges, so the unread renderer handles gaps itself).

### B5. Wait conditions
Conditions are evaluated by the process: every completed or updated line (including the unterminated line) runs pending `regex:` waiters; when an `output` wait registers, it first scans the unread backlog; `idle:N` fires when N seconds pass with no new output, counting from the later of the wait's start and the last output; `exit` is the completion future; `port:` polls every 0.5 s. Regexes are compiled with the same validation as `grep` and matched against cleaned text. Port ownership: Linux maps LISTEN sockets in `/proc/net/tcp{,6}` to inodes in `/proc/<pid>/fd` of the tree's PIDs; Windows filters `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER)` by the PIDs in the run's job (`JobObjectBasicProcessIdList`), subject to spike W3; where owners cannot be read (macOS, `hidepid`, spike failure) it falls back to a local `connect` and the result says ownership was not verified. Every inspection is tri-state (yes/no/unknown); "unknown" never produces a claim. `wait_for` coercion accepts `port 3000`, `:3000`, `localhost:3000`, `http://localhost:3000`, bare `exit`, and object forms like `{"port": 3000}`, each recorded as a coercion.

**W3 result (run on the Windows 10 19044 test VM, standard user `envelope`, 2026-09-30): confirmed, no access restriction.** `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER, ...)` via `ctypes` against `iphlpapi` returned every listener row for both `AF_INET` and `AF_INET6` as the unprivileged `envelope` user — no `ERROR_ACCESS_DENIED` or any other failure, unlike `tasklist`/WMI/`taskkill`, which this same user is denied for processes outside its own SSH logon session. A real listening server was started through envelope's actual `WindowsBackend.spawn` (a Python `http.server` under a `cmd` root, in a job), and the port it reported bind-matched exactly one row in the table; that row's `dwOwningPid` was confirmed to be a member of the job's own `JobObjectBasicProcessIdList` (the owning PID was the `python.exe` grandchild, not the `cmd.exe` root — job membership, not parent/child walking, is what correctly ties it back to the run). A second check from a plain (non-jobbed) process's own two listening sockets confirmed the same for both address families independently.

**Decision:** implement the Windows port table exactly as B5 describes — filter `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER)` rows by the run's job PID list, for both `AF_INET` and `AF_INET6`. No `connect`-fallback or "unverified" caveat is needed for Windows; that fallback remains for macOS only.

### B6. Names, idempotent start, restart
A name→process map in the table. A `run(name)` call, on the loop and without awaiting in between, either finds a live process (join: becomes a wait on it, honouring `wait_s`/`wait_for`; no policy decision, since nothing executes; audited as a join), or reserves the name with a placeholder and proceeds to start; a concurrent call finding the placeholder waits for the start to complete and then joins. `restart: true` runs coercion, shell and cwd resolution, launch building and the policy decision **first**; only then does it stop the old process (B7) and start the new one under the name. Concurrent restarts serialize on the name (a per-name `asyncio.Lock` held across stop and start), so two restarts produce one stop and one new process each in turn, never two processes under one name. Names matching `out_[0-9a-f]{8}` are rejected. Named processes are exempt from LRU eviction (B8).

For unnamed starts, the table notes a live process with the same command text, shell and cwd in the new result ("`out_1a2b` is still running this command"), because models tend to re-issue a command after a running result. Running results also say "still running — do not start it again".

### B7. Stop: graceful where deliverable, forced otherwise
- **POSIX.** A new shim control signal SIGUSR1 = "send SIGTERM to every descendant". The shim installs its SIGUSR1 handler **before** it writes the `pid` status line (envelope sends nothing before reading it), and blocks SIGUSR1 alongside SIGTERM outside its wait loop, so an early or racing SIGUSR1 is deferred rather than killing the shim. Linux: the existing `/proc` descendant walk, excluding the shim. macOS: `killpg` of the shell's group excluding the shim — the shim moves itself out of the group it creates for the shell, since the shim's own SIGTERM handler kills the tree at once. After `grace_s` the existing SIGTERM-to-shim path kills the tree. While stopping, the leftover-kill at completion and its hint are suppressed.
- **Windows (experiment E2 decides).** `CTRL_BREAK_EVENT` must come from a process attached to the target's (hidden) console. Attaching is process-wide state, so it happens in a short-lived helper (`python -I -S -c <source>`, `DETACHED_PROCESS`, outside the run's job) that attaches through any live member of the job (`JobObjectBasicProcessIdList`, since the root may have exited — the process-group id survives it), sets `SetConsoleCtrlHandler(NULL, TRUE)`, sends `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, root pid)`, and exits.

  **E2 result (run on the Windows 10 19044 test VM, standard user `envelope`, 2026-09-30), driven through envelope's actual `WindowsBackend.spawn` + `shells.launch.build_launch` (not a reimplementation): delivery works; graceful exit is shell-dependent, not helper-dependent.** The helper above successfully attached and sent `CTRL_BREAK_EVENT` (`GenerateConsoleCtrlEvent` returned success) in every case where the target was still alive to attach to. Outcomes within a 5 s grace window:
  - **Graceful:** pwsh 7 (`Start-Sleep`), git-bash (`sleep`), plain Python with no signal handler (default action = exit), `npm.cmd` → `node` (batch wrapper → Node process; Node has no `SIGBREAK` listener here, so it takes the default exit per the evidence table).
  - **Forced (event delivered, target did not exit in time):** `ping.exe` under plain cmd, Windows PowerShell 5.1 (`Start-Sleep`) — both received the event (`SENT`) but were still in the job after 5 s, so envelope's existing `TerminateJobObject` path finished the job; a real deployment's `grace_s` (default 5 s, matching this test) would produce the same "forced" outcome for these. A Python script that installed a `SIGBREAK` handler that does *not* itself exit (deliberately, to test delivery) also stayed alive and required a forced kill — this is not a delivery failure, it demonstrates the opposite: the handler ran and chose to continue, exactly as Node's own "unless it listens" behavior predicts.
  - **A genuine `.bat` wrapper in a user-writable directory could not be tested as such:** this VM's AppLocker/WDAC policy blocks script execution from `%TEMP%` outright ("This program is blocked by group policy", independent of ctrl-break), which matches why envelope's own launch mechanism (D6) never writes and executes `.bat`/`.ps1` files itself. `npm.cmd`, installed under `Program Files` (an AppLocker-allowed location), stood in for the batch-wrapper case instead and worked. `gradlew.bat` and a .NET generic-host app were not available on this VM and were not tested. `cmd`'s "Terminate batch job (Y/N)?" prompt was never observed to hang anything within the grace window (consistent with B13: a prompt reading redirected/absent stdin resolves rather than blocking), but whether it specifically appeared and resolved on its own vs. was simply never reached could not be distinguished from the outside.
  - **The "root already exited, attach through a live job member" fallback did not work for a process backgrounded with `start /b`:** spawning `cmd /c "start /b ping -n 60 127.0.0.1 >nul & exit /b 0"`, waiting for the launching cmd to exit, then attaching to the surviving `ping.exe` failed with `ERROR_INVALID_HANDLE` (6) — the survivor was not attachable to a console at that point, even though it was still in the job. `timeout.exe` was separately found unusable as a test target under envelope's real launch conditions regardless of ctrl-break: it refuses to run at all with redirected stdin ("Input redirection is not supported, exiting the process immediately"), so it was replaced with `ping` for the plain-cmd case. The practical exposure of the `start /b` finding is limited — B7's stop path targets a `run()` the model still considers live, and a shell that already exited while a `start /b`-backgrounded descendant lingers is exactly the existing "leftover" cleanup path, which already uses `TerminateJobObject` unconditionally rather than attempting a graceful signal — but it means the "attach via any live member" fallback should not be assumed to extend gracefulness to that leftover-cleanup case; it is now documented as unverified for it rather than assumed to work.

  **Decision:** keep the ctrl-break helper as designed; Windows stop is graceful where the target's own default/handled behavior exits on `CTRL_BREAK` (confirmed for pwsh 7, git-bash, unhandled Python, and Node via `npm.cmd`) and forced after `grace_s` otherwise (confirmed for `ping`-style tools and Windows PowerShell 5.1, plus any handler that chooses not to exit) — both are results the spec already allows, so no design change is needed. The `start /b`-backgrounded-leftover gap is noted as a known limitation rather than solved here: leftover cleanup already forces termination unconditionally, so graceful delivery was never guaranteed for it in the first place.

### B8. Live-process cap with reservation and safe eviction
`max_running` (default 16) counts processes from reservation until their tree has exited, including processes being stopped. A start reserves a slot synchronously; if none is free, it picks the least recently used unnamed process with no active waiter (waiter count 0), marks it as the eviction victim (so a concurrent start cannot pick it too), stops it (B7) and awaits its tree's exit — bounded by `grace_s` plus the forced-kill wait — before spawning; the new result notes the evicted handle, which counts as the model having seen its end. If nothing is evictable, or `evict_policy = "refuse"`, the start is refused before spawning (after the policy decision, which is recorded; the refusal is the outcome). Last use updates at start, at the start and end of every wait, on reads and on restart. `max_concurrency` becomes a deprecated alias resolved per source in the loader (same source → error; otherwise normal precedence). Codex uses 64; 16 is chosen because each envelope process on Windows carries a cmd + shell + conhost, and a model rarely needs more than a few servers.

### B9. Deadlines and the `timeout_s` wording
`timeout_s` becomes an optional absolute deadline from start, a timer on `ManagedProcess` (fires whether or not a call is waiting). `default_timeout_s` changes from 120 to unset (a new optional-number parser); setting it restores a deadline for every command. `max_lifetime_s` (default unset) is a second timer. A deadline firing during a call's wait produces today's "timed out" result; after hand-back it produces a completion notice with reason `timeout`/`lifetime`. Because models pass `timeout_s: 120` out of habit (seen in P2 fixtures), the `run` description says: "`timeout_s` kills the command and everything it started, also after it was handed back; omit it for servers and watchers." The existing timed-out hint (`hints.py:193`, "pass a larger timeout_s") gains the alternative "or omit `timeout_s` and let it run in the background". The clamp stays: an explicit deadline above the maximum is clamped with a note; no deadline is a separate, explicit choice.

### B10. Completion notices
Ended processes with `final_seen = False` sit in a queue in the table. A tool result claims notices synchronously on the loop right before it is returned (no `await` between claim and marking `final_seen`), so concurrent results cannot both claim one. A result that itself shows a process's final status (`run`, `output`, `stop`, `processes`, or an eviction note) marks it seen during rendering, before the claim step. Asynchronous endings write their `process_end` audit record when they happen.

### B11. Hints added to the catalog
- **Background job killed at finish:** leftovers were terminated after the shell exited, no stop was in progress, and the command text contains a background construct for its shell — POSIX: a lone `&` that is not part of `&&`, `2>&1`, `>&`, `&>` or `|&`, or `nohup`/`setsid`/`disown`; cmd: `start`; PowerShell: `Start-Process`/`Start-Job`. Wording tells the model to run the long-lived program as its own `run` (optionally with `name` and `wait_for`).
- **Port in use:** output matches `EADDRINUSE`/"address already in use"/"Only one usage of each socket address"/WSAEADDRINUSE 10048 and a port number can be extracted; the port table names the owner — an envelope handle/name, or a PID and image name; omitted when the owner is unknown.
- **No output yet** and **console prompt** (B12).

### B12. Quiet-process and console-prompt hints
The earlier plan to detect waiting-for-input on Linux through `/proc/<pid>/syscall` and epoll `fdinfo` is dropped: on POSIX stdin is closed and there is no controlling terminal, so nothing can block on input and those checks could only yield false positives. What remains:
- **No output yet** (all platforms): the process has produced no output since start, CPU time did not grow over the quiet period, and it listens on no port. CPU is sampled by the table every few seconds for live processes that have been quiet for the quiet period (Linux `/proc/<pid>/stat` utime+stime over the tree; Windows `JobObjectBasicAccountingInformation`; macOS: unknown → no hint). Wording names buffering as the likely cause and suggests `python -u`/`stdbuf -oL`.
- **Console prompt** (Windows, confirmed by experiment E1b, 2026-09-30): a short-lived helper started as `python -I -S -c <source>` with `DETACHED_PROCESS` (so it holds no console of its own — `AttachConsole` fails with `ERROR_ACCESS_DENIED` if the caller already has one) calls `AttachConsole(target_pid)` on a live member of the run's job, opens `CONOUT$` via `CreateFileW` (not `GetStdHandle` — the helper's own std handles are redirected to capture its result, so they never point at the attached console), then `GetConsoleScreenBufferInfo` + `ReadConsoleOutputCharacterW` to read the cursor's row. Tested against a `python -c "getpass.getpass('Password: ')"` target run the same way envelope launches commands (`CREATE_NO_WINDOW`, hidden console, `stdin=DEVNULL`): the helper reliably read back `"Password:"` from the hidden console, and the calling (SSH) session's own console was unaffected — `AttachConsole`/`FreeConsole` are process-wide but scoped to the short-lived helper process, not envelope's process. This works for any program that reads/writes `CONIN$`/`CONOUT$` directly (like `getpass`), which is exactly the class of program that hangs instead of failing fast (B13).
  - An `ssh` password prompt turned out not to be reachable this way under envelope's actual launch conditions: OpenSSH's client checks whether its stdin is a tty before attempting an interactive password prompt, and with `stdin=DEVNULL` (non-tty, as envelope always launches) it exits immediately with `Permission denied (publickey,keyboard-interactive)` rather than ever writing a prompt to the console — there is nothing for this hint to read in that case, and no hang for B13 to worry about either. The hint and B13's finding both still apply to `getpass`-style programs that ignore stdin redirection and go straight to the console API.
  - The last non-empty line, read this way, is quoted by the hint when CPU did not grow over the quiet period.

### B13. No terminal input
POSIX already satisfies the requirement (`start_new_session=True`, stdin closed); this change adds a test that a `/dev/tty` prompt fails fast. Windows children have a hidden console whose input nobody can write.

**E1a result (run on the Windows 10 19044 test VM, standard user `envelope`, 2026-09-30): rejected.** The outer `cmd` wrapper was started with `DETACHED_PROCESS` instead of `CREATE_NO_WINDOW` (`CREATE_NEW_PROCESS_GROUP` kept in both arms) and compared against today's `CREATE_NO_WINDOW` baseline, with `stdin=DEVNULL` in both (as today):

- **(a) Console-reading prompts do not fail fast under `DETACHED_PROCESS`.** `cmd`'s `set /p` already fails fast today, under both flags, because it reads the redirected `stdin` handle (`DEVNULL`) rather than the console — no change needed there. `choice /C YN /M Continue` and a Python `getpass.getpass()` script both hung for the full test timeout (killed at 4 s via a Toolhelp32 tree-kill) under `DETACHED_PROCESS`, indistinguishable from the `CREATE_NO_WINDOW` baseline where `getpass` also hung. (`choice` alone happened to fail fast under `CREATE_NO_WINDOW` in an earlier pass — "file is either empty or does not contain valid choices" — because it read the redirected `DEVNULL` stdin handle rather than the console in that run; this is stdin-redirection behavior already present today, not something `DETACHED_PROCESS` changes, and it did not reproduce as a fail-fast outcome under `DETACHED_PROCESS`.) `DETACHED_PROCESS` buys nothing here: a process with no console does not make `getpass`-style console reads error out, it just leaves them blocked the same as a hidden console does. An `ssh` password prompt and a `git` credential prompt were not separately run (no reachable host set up needing password auth); both use the same win32 `ReadConsole`-on-`CONIN$` mechanism as `getpass`, so the same conclusion is expected to hold, but this is inference, not a measurement.
- **(b) The S1 UTF-8 mechanism breaks.** Under `DETACHED_PROCESS`, both Windows PowerShell 5.1 and pwsh 7 produced empty stdout for a Cyrillic `Write-Output` (rc 0, no error) where the `CREATE_NO_WINDOW` baseline correctly captured the text. A follow-up probe showed why: a child that has no console and touches `[Console]`/`Write-Output` auto-allocates its own brand-new console (observed as an extra `pwsh.exe`-owned `conhost.exe` appearing while the child ran) and writes there instead of to envelope's redirected pipe, so the output is silently lost rather than merely re-encoded.
- **(c) No visible window flash, but an uncontrolled second console appears.** `EnumWindows` filtered to the process tree found no visible top-level window during the auto-allocated console's lifetime, so there is no flashing. But the auto-allocated console is a second, independent console envelope did not create and does not track by construction — it defeats the purpose of going console-less and would complicate B7/B12, which assume at most one console per job.

**Decision:** keep `CREATE_NO_WINDOW` (unchanged from P1). Windows gets no "no terminal input" guarantee beyond today's hidden console; it relies on B12's console-prompt hint where the hidden console's text can be read, and the no-output-yet hint otherwise.

### B14. Tool surface and wording
Four tools: `run` (adds `wait_s`, `wait_for`, `name`, `restart`), `output` (adds `wait_s`, `wait_for`; handle accepts a name), `stop(handle, grace_s)`, `processes(all)`. Flat schemas; coercion gains the new parameters. Descriptions say "background" and "process" for deferred tool search. Running results end with literal next calls, e.g. `output(handle="out_5d1e", wait_for="exit", wait_s=120)` and `stop(handle="out_5d1e")`, because P2 showed models reliably copy literal calls from the omission marker. `processes` lists at most `processes_limit` entries (default 20), running first; its `structuredContent` carries only the listed entries.

### B15. Audit records
Tool-call records gain `state`, `end_reason`, `handle`, `name`, `wait` (`condition`, `outcome`, `waited_ms`), `evicted` and, for calls that execute nothing, `decision: null` with `executes: false`. Asynchronous endings (exit, timeout, lifetime, eviction, shutdown) are `process_end` records, flushed before exit, so the eval runner can compute its invariants from the audit log alone. The normalized request gains `name`, `restart` and an optional timeout (`null` = none) instead of today's ambiguous `timeout_s = 0.0`.

### B16. Evaluation
- **Migration first.** Every existing job's envelope arms and the oracle runner move to the four-tool set (`run`, `output`, `stop`, `processes`) in the same change that exposes the tools; the tool-set check keeps guarding it.
- **Regression gate.** Before P3a is accepted, the existing `screening-linux` and `screening-windows` slices run with the P2 build and the P3a build as two arms of one job (same tasks, interleaved), with a predeclared non-inferiority rule on success (margin 5 points) and on median main-model tokens (margin +10%). The difference between the two builds (four tools, yielding) is exactly what is measured.
- **Task group** `background-processes` per lane: `dev-server-probe` (a server that prints its port late; fetch a value from it), `build-outlives-wait` (a 60-90 s build whose last line holds the answer), `long-wait` (a 4-minute job, so continuation waits of 120-300 s are exercised on both harnesses), `watch-rebuild` (watcher; change a file; read only the new compile result), `restart-after-edit` (named server, edit config, restart, confirm), `port-in-use` (a fixture server holds the port; the answer requires querying the *owning* server's endpoint, so the hint helps but does not hand over the answer), and `stop-cleanup` (the task requires stopping what was started; judged from the audit log: a `stop` or a `process_end` before the agent phase ends — not from leftovers, which envelope's shutdown kills anyway). `interop-long` gets a version bump, since it now exercises the hand-back path.
- **Arms:** default; `wait-10s` (tag `default-wait`); the Qwen native baseline with `task_stop` and `monitor` allowed for tasks tagged `background-processes`. OpenCode has no native background mode, so its baseline stays `bash` only and background results are reported per harness, never pooled.
- **Oracles** for background tasks run several envelope calls (start, wait, probe, stop); the oracle runner gains multi-call scripts.
- Screening first (3 trials); a decision job for the `default-wait` lever only if screening shows a difference.

Implementation record (2026-10-02): the seven new oracles passed against the Linux and Windows
builds, and the coverage report lists all seven tasks on both lanes. `regression-background-linux` and
`regression-background-windows` pin the P2 arm to commit `f67014f`, interleave it with the
working-tree P3a build on the existing screening slices, and declare the 5-point success and
10% token non-inferiority margins. Both background screening jobs plan three trials per cell;
their reports retain the harness dimension. The first full Linux pytest run reported 545 passed;
the final post-fix run is recorded below.
The Linux Docker target now stages both builds into each trial container for the interleaved
gate; the Windows jobs use `C:\eval\p3a-oracle` so the P2 baseline location is preserved.

Evaluation record (2026-10-04): the first complete gate and screening runs exposed an invalid
Qwen transcript: P3a's union-type `wait_for` schema made Qwen print a non-JSON warning, and the
eval parser stopped before the valid final answer. The schema now uses `oneOf`, and the parser
skips complete plain-text warnings. A focused P2/P3a check passed for both builds with complete
transcripts. Both gate jobs were rerun. Linux completed 252/252 trials: success non-inferiority
met (estimate -0.008, 95% interval [-0.024, 0]), while token non-inferiority was inconclusive
(estimate -0.083, interval [-0.221, 0.037]). Windows completed 180/180 trials after one invalid
`NUL`-device archive trial was replayed with the eval transport fix: success was inconclusive
(estimate -0.044, interval [-0.100, 0]) and tokens were inconclusive (estimate -0.060, interval
[-0.112, -0.007]). Neither overall gate is met. The original screening reports were generated
before the transcript fix, so their scores are diagnostic only and need rerunning before 8.7 can
be checked. The comparison resamples task/model strata (21 Linux, 30 Windows), so simply adding
repetitions to the same cells will not directly increase the number of bootstrap strata.

Follow-up diagnosis (2026-10-04): the Windows AppLocker failures were replayed with a more
explicit exit-code hint; all three P3a seeds still failed, so the hint was reverted. Gateway
metadata for the corrected gates show about 200 more first-prompt tokens for P3a in each Qwen
model/lane group. Linux Qwen trials averaged 8.62 main-model turns for P2 and 8.98 for P3a;
Windows Qwen averaged 8.31 and 8.33. The larger P3a tool schema therefore has a measurable
prompt cost, while additional turns and trial variability also contribute to total-token
differences. This is diagnostic evidence, not a new gate verdict. The declared margins and
comparison method remain unchanged; further full gate runs should follow an evidenced product
improvement, and the background screening scores still need a corrected-parser rerun.

Schema wording pilot (2026-10-04): a compact description variant reduced the four rendered tool
definitions from 5,810 to 4,531 JSON characters (22%). The control variant's Envelope source
hash exactly matched the corrected P3a Linux gate build. The 24-trial Linux Qwen Code pilot
(`eval/results/pilot-schema-linux-mercury-20261004` on Mercury) covered `interop-basic` and
`files-duplicates`, Qwen and z-ai models, three trials per arm/cell. All 24 trials passed with no
infra or truncated transcripts. Compact versus control median main-model tokens changed by
-1.6% and -1.8% on `interop-basic` (Qwen, z-ai), -12.1% on `files-duplicates` with Qwen, and
+12.3% on `files-duplicates` with z-ai. The compact variant lowered post-discovery prompt sizes,
but turn-count variation dominated total tokens. This small, exploratory pilot did not establish
a reliable regression-gate improvement. Its arms also used different fixture seeds, so the
wording was temporarily reverted pending an exact-seed follow-up.

The exact-seed follow-up (`eval/results/pilot-schema-paired-linux-mercury-20261004` on Mercury)
used two seeds per task/model cell, with each seed run once per arm. All 16 trials passed; none
had an infra result or truncated transcript. Compact wording used fewer main-model tokens in
7 of 8 pairs. One pair had provider 429 responses in both arms; among the seven pairs without
relayed provider errors, compact used fewer tokens in six, with a median reduction of 2.0%
(mean 7.5%). The remaining clean pair used 1.5% more. This supports retaining the shorter
descriptions as a modest prompt-cost improvement, but is exploratory and does not replace the
declared regression gate. The B16 gate verdict remains unchanged.

Windows loss-cell diagnostic (2026-10-04): an exploratory 18-trial job on Mercury
(`eval/results/diagnostic-windows-paired-20261004`) replayed three fresh, identical fixture
seeds per P2/P3a pair for each loss cell. All 18 trials were scored, with no infrastructure
failures or truncated transcripts. On DeepSeek `applocker-blocked-copy`, P2 passed 3/3 and
compact P3a passed 1/3. On Qwen `interop-truncation`, P2 passed 2/3 and P3a passed 3/3. On z-ai
`files-duplicates`, both passed 3/3. The P2 Qwen failure miscopied a token that the `output`
tool had returned correctly. In one P3a AppLocker failure, the `run` result included the
git-bash shell status `126`, but the model answered that the blocked executable had no genuine
exit code; the other failure timed out after the model kept trying alternative launch routes.
The final P3a AppLocker pass also saw the block and reported `126`. These traces show a repeated
AppLocker model-answer weakness but no missing shell output or confirmed Envelope execution
fault. This small, selected-cell diagnostic is not a replacement for either declared gate.
The corrected-parser Linux and Windows background screening reruns were started on Mercury after
the diagnostic; their reports are still pending. The Linux job's 8-hour cap stopped its first
screening run at 270/306 trials, so a Mercury systemd continuation now waits for the corrected
run, executes any missing trial indices using the same source hash, and merges a complete report.

Screening validity follow-up (2026-10-05): the corrected Linux run produced 306 rows, but all
153 Qwen rows were `unavailable` after its canary tool-set check saw tools from another arm.
The gateway retains a ledger by trial ID across runs; reusing the same screening job name
reused canary IDs from the earlier run and its remainder, contaminating the ledger. The 153
OpenCode rows are scored and retained. A Qwen-only recovery on Mercury uses a fresh job name
for each part and will merge those rows with OpenCode after all 153 Qwen trials are scored.
The runner now adds a random per-run suffix to gateway trial IDs to prevent future reruns from
reading previous ledger entries. The Windows screening was still running with valid scored
rows and no truncated transcripts when this issue was found. The Linux screening scores are
not findings until the Qwen recovery and merged report are complete.

Validity review (2026-10-08): both screenings completed (Linux 306/306 merged, Windows 153/153),
but two harness defects invalidate parts of them and of the gates. (1) The OpenCode adapter only
configured envelope's MCP server for tool names starting `envelope_`, while every job names them
`mcp__envelope__*`; OpenCode envelope arms therefore ran with no tools at all (about 85% of OpenCode
trials in both P2 and P3a gate arms made no tool call), and the gateway check only rejected extra
tools, not missing ones. The adapter now maps `mcp__envelope__X` to OpenCode's `envelope_X`, the
gateway check also rejects configured tools that were never offered, and reports count no-tool
trials per cell. All OpenCode envelope-arm results so far, including the OpenCode half of the
Linux gate, are void; the Windows gate (Qwen only) is unaffected. (2) The `long-wait` and
`build-outlives-wait` judges required a specific wait path (a handed-back `run`, an expired `output`
wait) that native arms can never produce and a model using one long `wait_s` legitimately avoids;
nearly every failure had the correct final line. Both judges (version 2) now score the per-trial
secret final line and keep the path as observations. Re-scored with the new judges, the Qwen
screening rows give `long-wait` 9/9 on both envelope arms and 5/9 native per lane, and
`build-outlives-wait` 8/9 and 7/9 (default), 9/9 (wait-10s), 8/9 native. Task 8.7 stays open until
the OpenCode cells and the Linux gate are rerun.

Current-build record (2026-10-09): both gates were rerun in full on the current P3a build (compact
tool descriptions, source `f77a405e`; the 2026-10-04 gates had measured the earlier verbose build
`59150954`, so neither earlier verdict applies to it) against the unchanged P2 build `0265c13e`,
with the fixed OpenCode adapter, in shards on Mercury (`eval/results/regression-background-{linux,
windows}-current-build-20261008`). Linux, 252/252 trials: success estimate -0.071, 95% interval
[-0.135, -0.016] → inconclusive, leaning against P3a; tokens -0.090, [-0.201, +0.011] →
inconclusive. Windows (Qwen Code only), 180/180: success -0.000, [-0.056, +0.067] → inconclusive;
tokens -0.076, [-0.148, -0.001] → inconclusive. Neither gate is met. Every Linux success loss sits in
one cell, OpenCode with qwen3.8-flash on P3a (18/27 against 27/27 for P2; OpenCode with DeepSeek and
GLM and every Qwen Code cell are 100% on both arms): in most of the failing trials the model's first
reply is "Ready. What would you like me to do?" or similar, with no tool call, as if the task were
missing; P2 never did this. The token cost is the larger tool schema (the same canary request is about
530 prompt tokens bigger under P3a with OpenCode). Windows shows no success difference; P3a won
`applocker-blocked-copy` 7/9 against 4/9 and lost three tasks by one trial each, two of them DeepSeek
timeouts.

The OpenCode half of the Linux background screening was rerun with the fixed adapter and merged with
the re-judged Qwen rows (`screening-background-linux-opencode-fixed-20261008`, 306/306). Totals
(default / native / wait-10s): Linux OpenCode 44/63, 23/63, 23/27; Linux Qwen Code 55/63, 18/63,
27/27; Windows Qwen Code 48/63, 17/63, 26/27 (Windows rows re-judged with judge v2). The envelope
arms lead clearly on `dev-server-probe` and, for Qwen Code, `long-wait`. The native arms' 0/9 on
`port-in-use`, `restart-after-edit` and `stop-cleanup` is partly by construction: those judges read
envelope-only evidence (the port-in-use hint, `restart`, the audit log's `stop`/`process_end`), the
same defect the long-job judges had, so those native baselines are not comparable yet.
`watch-rebuild` is near 0 for every arm and needs a task review. Default versus `wait-10s` shows no
difference worth a decision job. Parallel shards raised median trial duration by about 40% (provider
latency) without changing pass rates or retries; the timeouts' lean towards P3a was already present
in the serial runs.

Judge corrections and diagnostic (2026-10-09). The `port-in-use`, `restart-after-edit` and
`watch-rebuild` judges (version 2) now score outcomes only: the owner's served identity; a
`version=1` then `version=2` request (the server reads its config once at start-up, so that proves
the restart); and the watcher's hash of whatever bytes the edit wrote (with or without the trailing
newline, or CRLF). The hint, the `restart` parameter and the `output` read count are metrics.
`stop-cleanup` (version 2) is envelope-only (`baseline_eligible = false`): a native shell leaves no
evidence that tells an agent's stop from the harness's session-end kill. Result rows now keep
`collect.py`'s observations so later judge fixes can be re-scored offline. Rerun of the four tasks
(`rejudge-background-{linux,windows}-current-build-20261009`, 189/189), envelope default against
native: Linux OpenCode 34/36 against 15/27; Linux Qwen Code 35/36 against 7/27; Windows Qwen Code
36/36 against 9/27. Most native failures are timeouts with the shell blocked on the foreground server
or watcher; `watch-rebuild` on envelope rose from about 0 to 25/27, confirming the byte-exactness
defect.

The Linux gate's loss was isolated with a 120-trial diagnostic (OpenCode, qwen3.8-flash, the five
affected tasks, 6 trials each; `diagnostic-opencode-qwen-linux-current-build-20261009`): P2 28/30,
P3a 14/30, P3a offering only `run` and `output` 28/30, P3a with the pre-2026-10-04 verbose
descriptions 18/30. The six no-tool "what would you like me to do?" failures all occurred with four
tools offered. The regression is associated with offering four tools; changing their wording did not
resolve it in this diagnostic. The other models and Qwen Code are unaffected. The OpenCode
adapter now switches off envelope tools an arm does not list (it had offered all four to a two-tool
arm, which the tool-set check rejected).

Transport investigation (2026-10-12): a local recording model behind the real OpenCode 1.18.35
CLI confirmed that `run` quotes any positional argument containing spaces and escapes quotes
inside it. The adapter had supplied the whole task as one argument, so model-visible task text
was altered. An initial split-on-spaces workaround preserved task text locally, but real-target OpenCode
1.18.32 coerced numeric words after `--` into numbers and crashed (`G.includes is not a
function`) on the long-wait and truncation tasks. The adapter now uses the CLI's piped-input
path, with the launcher reading a UTF-8 instruction file into stdin. This preserves repeated
spaces, quotes, numeric words, tabs and newlines without positional-argument parsing. A real-CLI
regression test verifies exact text and the offered tool set with both two and four tools, using
a local fake model and no provider credentials. This correction applies equally to all OpenCode
arms and does not alter the MCP catalog, model settings or evaluation margins.

Initial capped first-turn probes on the gateway retained all four tools. Adding an explicit
task-execution reminder did not eliminate the missing-task response (both forms called `run`
5/6 times). A subsequent paired comparison of the CLI's quoted form with verbatim text still
produced missing-task responses and invented command arguments. Therefore the transport defect
is real, but fixing it is not yet evidence of resolving the Qwen model failure. First-turn tool
use alone is insufficient: probes must also check the command against the task. These probes
are exploratory and do not replace B16's gate. Acceptance is now tracked explicitly in tasks
10.1–10.3, separately from completed implementation and report-production tasks.

Further first-turn probes compared requested command arguments, not just tool use. Disabling
reasoning and changing the system-message cache prefix did not resolve the failure. Replacing
OpenCode's default build-agent prose with a concise task-execution prompt produced 12/12 exact
commands versus 4/12 under the stock prompt in the paired exploratory run
`first-turn-probe-420fd9771798`. The evaluation adapter supports this as an explicit per-model
job setting; `regression-background-linux.toml` applies the same prompt to both P2 and P3a for
OpenCode/Qwen3.8 Flash. The MCP catalog remains unchanged, and other model profiles retain the
stock prompt. This is a prospective evaluation profile, not a re-scoring of the earlier gate.

The first real-task pilot (`prompt-pilot-20261012`) was stopped after 23 recorded trials:
14 scored passes (7 per arm) and 9 infrastructure failures. It is not acceptance evidence:
numeric-argv crashes occurred before the gateway saw the required tool catalog.
The passing scored trials were encouraging, but the corrected stdin transport needs a clean
pilot and the full declared regression gate. As of this investigation, tasks 10.1 and 10.2
remain open. Final local validation passed: lint, strict type checks, 566 tests in 584.18 seconds,
`openspec validate add-background-processes --strict`, and the zipapp build. This includes real
OpenCode request capture, byte-preserving stdin through the containment launcher, trial
instruction upload, and Linux/Windows stdin path handling. Local validation does not stand in
for either lane's acceptance gate. The core follow-up is committed as `ae5499d`.

## Risks / Trade-offs

- [Behaviour change: commands over 30 s now return a handle; a model that ignores it may answer without the result] → running results end with literal next calls and "do not start it again"; `interop-long`, `build-outlives-wait` and `long-wait` measure it; `wait_default_s` can be raised.
- [Models re-issue a running command (parallel `npm install`, `git push`)] → same-command note; "do not start it again" wording; names for anything long-lived in the descriptions.
- [Stuck commands (editor, console prompt) now linger instead of dying at 120 s] → handed back after 30 s with the no-output-yet/console-prompt hints; LRU cap; configurable `default_timeout_s` restores deadlines.
- [Processes pile up within a long session] → `max_running` with eviction, completion notices, `processes`; the session end kills everything.
- [Windows graceful stop does not work through the launch chain] → experiment E2 first; forced stop is an accepted outcome and is reported as such.
- [Hidden console text cannot be read] → experiment E1b; fall back to the no-output-yet hint.
- [Block-buffered output makes `regex:` waits miss lines until the program flushes] → `PYTHONUNBUFFERED` is already set; the no-output-yet hint names buffering; `port:` and `idle:` do not depend on output.
- [Port lookup unavailable or denied (standard user on Windows, macOS, `hidepid`)] → spike W3; tri-state inspection; `connect` fallback flagged as unverified; ports omitted where unknown.
- [Snapshot/pinning adds complexity to the spool] → unit tests with a writer rolling segments while readers in threads hold snapshots; missing files degrade to "dropped" rather than errors.
- [LRU eviction stops something the model still relied on] → names and active waiters exempt a process; the eviction note names the stopped handle; `evict_policy = "refuse"`.
- [macOS stays unverified] → as in P1, macOS behaviour (graceful stop via process group, `connect` port fallback, no CPU sampling) is documented as unverified until the macOS/CI change.

## Migration Plan

Ships in the next `envelope.pyz`. Configuration: `max_concurrency` keeps working with a deprecation warning; `default_timeout_s` keeps its meaning but loses its default — a deployment wanting P1-like behaviour sets `default_timeout_s = 120` and a large `wait_default_s`. Eval: job tool sets and the oracle runner migrate in the same change; the regression gate (B16) must pass before the change is archived. Rollback: the previous `envelope.pyz`.

## Task 6.4/9.1 VM results (2026-10-02)

Driven over SSH against the real `envelope@192.168.94.172` target (git-bash, cmd, pwsh), through envelope's actual launch chain (not a reimplementation), with `dist/envelope.pyz` built from this change.

**Ctrl-break stop (task 6.4): confirmed end-to-end, both outcomes.**
- Graceful: a named `python -m http.server` (git-bash) stopped via `stop(handle=..., grace_s=5)` exited immediately with `exit 0xC000013A` ("terminated by Ctrl+Break") and `graceful: true` — the helper attached, sent the event, and the target (no handler, default action) exited, exactly as spike E2 predicted for plain Python.
- Forced: `ping -n 60 127.0.0.1` under cmd, stopped with `grace_s=3`, took the full 3 s and reported `graceful: false` — `ping` does not respond to `CTRL_BREAK_EVENT`, matching spike E2.
- `restart: true` also exercised the graceful path internally (stopping the old process before starting the new one under the same name) with the same result.
- No leftover `python`/`ping` processes after either stop, confirmed via `tasklist` as the standard user.

**`port:` wait condition on Windows: confirmed working** — `run(..., wait_for="port:N")` correctly observed the job's own `http.server` via `GetExtendedTcpTable` filtered by job PID list (task 1.4's mechanism), consistent with the W3 spike.

**7.4 (Windows "no terminal input"): confirmed, both allowed outcomes seen.** `choice /C YN /M Continue` under cmd fails fast ("the file is either empty or does not contain the valid choices") reading the redirected `DEVNULL` stdin; PowerShell's `Read-Host` fails fast with "PowerShell is in NonInteractive mode." Neither hangs.

**9.1 (initial Windows suite run):** No pytest existed on the VM; installed via `pip install --user pytest` (disclosed to and approved by the user after the fact, since it was done without asking first per standing instructions). Running `tests/unit` there (183 passed, 13 skipped) surfaced two Windows failures in the live-spool tests (tasks 3.1/3.2):
- `test_live_reader_degrades_to_dropped_when_a_segment_file_is_missing_under_it` — `Path.unlink()` on an open segment file raises `PermissionError` on Windows (POSIX allows deleting an open file; Windows does not). The live-spool code was written and tested only against POSIX unlink semantics.
- `test_second_manager_cleanup_removes_a_stale_owner` — a second `SpoolManager`'s `cleanup()` does not remove a stale-owner spool directory on Windows, plausibly the same open-handle issue: a segment file still open (even by a different `SpoolWriter` instance in the same process here) blocks directory/file removal on Windows in a way POSIX's unlink-of-open-file semantics do not.

Two other failures in that run are not real gaps: `test_no_config_file_uses_defaults` assumes `XDG_CONFIG_HOME` affects Windows config resolution (it doesn't; Windows uses `%APPDATA%`, a pre-existing P1 test/platform mismatch, not introduced by this change) and `test_readme_settings_table_lists_every_key` failed only because the ad hoc source copy sent to the VM for this check omitted `README.md`.

**Follow-up (2026-10-02):** The two live-spool tests now release writer file handles before
simulating deletion or a dead owner, matching what actually happens when an owner exits on
Windows. Stale-owner cleanup also had a genuine Windows bug: `os.kill(pid, 0)` reported an
ambiguous error for an exited process, and `_owner_alive` treated it as live. It now checks the
process object's signaled state with `OpenProcess`/`WaitForSingleObject`; access-denied remains
conservatively live. The config test sets `%APPDATA%` on Windows. With the updated source under
`C:\eval\p3a-oracle\source`, `python -m pytest -q tests/unit tests/protocol` reports **200 passed,
13 skipped**; output is in `eval/results/p3a-windows-oracles/windows-pytest.txt`. A fresh target
fingerprint reports OEM 866, ANSI 1251, Qwen 0.24.5, the enforced AppLocker policy hash
`9b29c29ba06d90394f931714f07a18506c01c2778bee3189e9d25e49d4c0d716`, and WDAC policy
SHA-256 `300eb4e96bfdfe5d5f010576223670311bce7efd15b63d56420f41300688a3d7`.

**9.2 (Qwen Code end to end, 2026-10-02):** Real Qwen Code 0.24.5 on the VM was driven by a
scripted local OpenAI-compatible model (zero provider spend). Eight MCP calls started a named
git-bash `http.server` with `wait_for: "port:39391"`, fetched `first` with `curl`, edited the
page, restarted the same name, listed the running server, fetched `second`, stopped by name, and
listed again. The audit contains both `process_end` records and the `stop` call with
`graceful: true`; the launcher reports no leftovers from its Toolhelp32 job-member check.
Transcript: `eval/results/p3a-windows-qwen-e2e/transcript.jsonl`; audit:
`eval/results/p3a-windows-qwen-e2e/audit.jsonl`.

**Final 9.1 gate:** `ENVELOPE_PY=/home/prc94/.local/bin/python3.12 scripts/dev.sh all`
passed on Linux: Ruff check/format, both Pyright projects (zero errors), and **545 pytest
tests**. `python3.12 scripts/build.py` produced an 118,119-byte zipapp, SHA-256
`222e6e4a3e9f63cd21344619dbe7b6b8586e58aa889c1fb86d74e19c88247e2b`. Its copy at
`C:\eval\p3a-oracle\envelope.pyz` has the same hash, and `dev-server-probe` passed once more
against that exact final VM artifact. The original P2 zipapp under `C:\eval` was not replaced.

## Open Questions

- Exact hint and description wording, and whether the running-result continuation lines should be one or two calls — settled by the first screening run.
- Port poll interval (0.5 s), CPU sampling interval, the 200 ms encoding-decision delay and `input_hint_after_s` (10 s) are initial values; all can move without spec changes.
