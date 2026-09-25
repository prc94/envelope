## 1. Project setup

- [x] 1.1 Create the `envelope` package layout (`protocol/`, `tools/`, `shells/`, `execution/` with `posix` and `windows` backends, `output/`, `render/`, `policy/`, `config/`, `doctor/`, `audit`) with a `__main__` entry point and verify `python -m envelope --version` prints the version
- [x] 1.2 Configure dev tooling (pytest, pyright strict, ruff) in `pyproject.toml` with zero runtime dependencies and verify `ruff check`, `pyright` and an empty `pytest` run pass
- [x] 1.3 Add a startup interpreter check that exits non-zero with a message on Python < 3.12 and verify with a unit test that simulates an older `sys.version_info`

## 2. P0 spikes (time-boxed; record each outcome in design.md D14)

- [x] 2.1 S1: prototype PowerShell script-as-data delivery (data file + `Get-Content -Raw` + script block, and length-framed stdin) on pwsh 7 and 5.1 in FullLanguage and CLM (`__PSLockdownPolicy=4`); verify a 100 KB script runs and caller stdin reaches it; record the chosen mechanism
- [x] 2.2 S2: prototype Windows containment (envelope self-job with kill-on-close, nested per-run job, `CREATE_SUSPENDED` + resume, `CREATE_NO_WINDOW`, `CREATE_NEW_PROCESS_GROUP`); verify a git-bash → node → child tree is killed per run and after `taskkill /F` of the prototype; record the resume mechanism
- [ ] 2.3 S3: prototype POSIX containment (setsid + killpg, Linux subreaper + PDEATHSIG shim, macOS kqueue watchdog shim); verify trees die on timeout and on SIGKILL of the parent, and measure shim overhead; record the recipe per OS
- [x] 2.4 S4: determine the git-bash launch recipe (`bin\bash.exe` login vs non-login, PATH contents, cwd translation, Windows tool visibility, startup latency); verify `git --version`, `ls` and `where.exe` work from the recipe; record it
- [x] 2.5 S5: build a stub stdio MCP server exposing a dummy `run` tool and connect OpenCode and Qwen Code to it; verify both list and call the tool, that progress notifications keep OpenCode's 30 s timeout alive, and whether the MCP conformance suite can drive stdio; record findings
- [x] 2.6 S6: capture sample output from cmd (with and without `chcp 65001`), Windows PowerShell 5.1 and a UTF-16 emitting tool; verify the decoding rules per shell against the samples and save them as test fixtures

## 3. Configuration and audit

- [x] 3.1 Implement the settings model with built-in defaults for every setting in the configuration spec and verify with unit tests that defaults match the specs (timeouts, budgets, spool limits, concurrency, env defaults, scrub patterns)
- [x] 3.2 Implement config loading from per-user TOML (`--config`/`ENVELOPE_CONFIG` override), `ENVELOPE_*` env vars and CLI flags with the specified precedence; verify with tests covering each layer overriding the previous
- [x] 3.3 Implement validation (unknown key warnings, fatal invalid values naming key/value/expected form); verify with tests for the `max_timeout_s = "forever"` and `defualt_shell` scenarios
- [x] 3.4 Implement stderr diagnostic logging with levels and verify nothing is ever written to stdout by the logger (test captures stdout)
- [x] 3.5 Implement the JSONL audit log (record schema from the configuration spec, per-user state location, non-fatal on write failure with a single stderr error); verify with tests for a normal record and an unwritable path

## 4. MCP protocol layer

- [x] 4.1 Implement newline-delimited JSON-RPC 2.0 framing and asyncio dispatch over stdin/stdout, with shutdown on stdin EOF; verify with a scripted client test including malformed input returning JSON-RPC errors
- [x] 4.2 Implement `2025-11-25` lifecycle (`initialize`, `notifications/initialized`, `ping`) including instructions in the initialize result; verify with a protocol test of the legacy handshake scenario
- [x] 4.3 Implement `2026-07-28` handling (`server/discover`, per-request `_meta` protocol version and client capabilities, server info in result `_meta`, `resultType: "complete"`, `UnsupportedProtocolVersion` error `-32022`); verify with protocol tests for the stateless, discovery and unsupported-version scenarios
- [x] 4.4 Implement `tools/list` with deterministic order, annotations and (for `2026-07-28`) `ttlMs`/`cacheScope`; verify two listings are byte-identical
- [x] 4.5 Implement `tools/call` dispatch with tool-error vs JSON-RPC-error semantics; verify unknown tool, invalid params and tool-level errors produce the specified shapes
- [x] 4.6 Implement progress notifications (≥ every 5 s when a progress token is present, monotonic progress, status message) and verify with a test using a 12 s command that at least 2 notifications arrive and none arrive without a token
- [x] 4.7 Implement `notifications/cancelled` handling that triggers process-tree termination and suppresses the response; verify with a test cancelling a running command

## 5. Shell registry

- [x] 5.1 Implement detection of built-in shells with parallel probes, versions and a 5 s total budget, marking failed/timed-out probes unavailable with reasons; verify on each CI OS that expected shells are detected and a hanging fake shell is reported as timed out
- [x] 5.2 Implement git-bash discovery (registry, `git.exe` location, standard dirs, `bin\bash.exe` launcher, WSL `System32\bash.exe` excluded) using the S4 recipe; verify with unit tests over fake filesystem/registry layouts and on the Windows CI runner
- [x] 5.3 Implement real-executable resolution that rejects `.bat`/`.cmd` shims; verify with a test where only `pwsh.cmd` exists on PATH
- [x] 5.4 Implement default shell selection per OS with config override, and shell aliases (case-insensitive, `bash` → `git-bash` on Windows); verify with unit tests for the Windows-without-Git, configured-default and alias scenarios
- [x] 5.5 Implement custom shell definitions (path, argument template with `{script}`/`{file}`, extension) and verify a config-defined shell runs a command in an integration test
- [x] 5.6 Implement in-process path translation between Windows, forward-slash and MSYS forms; verify with unit tests including drive letters, UNC paths and spaces

## 6. Command execution

- [x] 6.1 Implement per-shell launch builders: bash-family script file read as data, PowerShell script-as-data per S1 with prologue/epilogue (progress off, UTF-8, plain-text rendering, exit-code mapping, non-terminating error flag), cmd with `/d /s /c` + `chcp 65001` and the 8,191-char rejection; verify with integration tests for the 100 KB bash script, PowerShell exit-code scenarios and oversized cmd command
- [x] 6.2 Implement the environment builder (inherit, scrub patterns, non-interactive defaults, explicit `env` precedence and exemption from scrubbing); verify with tests for the `OPENROUTER_API_KEY`, pager and `TERM` override scenarios
- [x] 6.3 Implement cwd resolution (default working directory, translation per shell, missing-directory tool error); verify with integration tests for git-bash and pwsh path scenarios
- [x] 6.4 Implement stdin handling (closed by default, provided text then EOF); verify the prompting-command and `wc -l` scenarios
- [ ] 6.5 Implement the POSIX execution backend per S3 (new session, killpg, Linux subreaper + PDEATHSIG shim, macOS watchdog); verify tree kill on timeout and survival-free cleanup after SIGKILL of envelope in integration tests on Linux and macOS
- [x] 6.6 Implement the Windows execution backend per S2 (self-job with kill-on-close, nested per-run jobs, suspended create + resume, window and process-group flags); verify the grandchild-kill and `taskkill /F` crash-safety scenarios on the Windows CI runner
- [x] 6.7 Implement timeouts (default 120 s, clamp to max with a note, tree termination, partial output); verify the `sleep 30` / `timeout_s: 2` and clamping scenarios
- [x] 6.8 Implement the concurrency limiter (default 8, waiting time counted toward timeout); verify with a test issuing 10 concurrent calls and observing at most 8 running
- [x] 6.9 Implement spawn-failure reporting including application-control errors 1260/4551; verify with a test using a non-executable/blocked shell path and a unit test mapping the Windows error codes

## 7. Output pipeline

- [x] 7.1 Implement stream readers producing an ordered, stream-tagged chunk sequence and the framed spool file with an incremental line-offset index; verify the a/b/c interleaving scenario and that line lookups by index match a full re-read
- [x] 7.2 Implement spool cap (keep head and tail, record dropped bytes), total spool size eviction, 24 h retention cleanup at startup and `out_<8 hex>` handles; verify with tests for the 200 MiB/64 MiB scenario and eviction order
- [x] 7.3 Implement decoding (UTF-8, UTF-16 BOM/pattern detection, console code page for cmd/Windows PowerShell, replacement with note) using the S6 fixtures; verify the UTF-16 and CP850 "Größe" scenarios
- [x] 7.4 Implement binary detection with byte count and type sniffing; verify the PNG scenario
- [x] 7.5 Implement cleanup (ANSI strip, `\r` and backspace resolution, repeat folding at ≥3); verify the progress-bar and 312-repeat scenarios while the spool keeps raw bytes
- [x] 7.6 Implement budgeted head/tail truncation (12,000 chars / 400 lines default, tail-favoring split, omission marker with exact `output(...)` calls); verify the 18,000-line and 20-line scenarios
- [x] 7.7 Implement the `output` tool read modes (tail default, offset/limit, grep with context and match counts, stream filter, invalid regex error, unknown/expired handle error); verify each scenario in the output-pipeline spec

## 8. Result rendering and hints

- [x] 8.1 Implement the text result layout (header, truncation line, body, `(no output)`, notes) and verify with snapshot tests for success, failure, timeout, cancellation and empty-output results
- [x] 8.2 Implement metadata-only `structuredContent` for `run` and `output`; verify with a test that it never contains output text and stays under 1,000 characters for a typical result
- [x] 8.3 Implement the hint catalog (127/not recognized with cross-shell PATH lookup, 126, signals incl. SIGKILL/OOM note, Windows crash codes, timeout, application control, CLM, execution policy, PowerShell errors with exit 0, binary, replacement chars, stdin-closed prompt); verify each hint with a unit test and the "no hint without certainty" scenario

## 9. Execution policy seam

- [x] 9.1 Implement the normalized request model (`exec` and `input` kinds) and the single execution gateway that all execution must pass through; verify with a test that `run` cannot start a process without a recorded decision
- [x] 9.2 Implement decisions (allow/ask/deny with rule id and reasons) and the default policy limits (allowed shells, cwd roots, max length); verify the cwd-outside-roots and default-allow scenarios
- [x] 9.3 Implement deny/ask rendering as actionable tool errors and audit-only mode; verify the denied-command, ask-without-elicitation and audit-only scenarios

## 10. Tools and argument coercion

- [x] 10.1 Implement the coercion layer (numeric strings, boolean strings, JSON-encoded objects/arrays, `KEY=value` env lines, audit-logged coercions, precise errors for uncoercible values); verify the three coercion scenarios in the mcp-server spec
- [x] 10.2 Implement the `run` tool end to end (schema, description with detected shells and search keywords, annotations, registry → policy → execution → output → rendering, audit record); verify with an integration test calling `run` via the protocol on each CI OS
- [x] 10.3 Implement the `output` tool end to end via the protocol; verify paging and grep on a spooled log through a protocol-level test
- [x] 10.4 Implement generated server instructions (OS, shells and versions, default shell, PowerShell language mode and execution policy, git-bash notes, truncation recovery, ≤ 2,000 chars); verify with a test on a simulated Windows/CLM environment

## 11. Doctor command

- [x] 11.1 Implement `doctor` (version, interpreter, OS/arch, config paths and warnings, shells, default shell, spool/audit paths and writability) with exit codes; verify the Linux scenario in CI
- [x] 11.2 Implement the Windows restriction report (language mode, execution policy, code pages, per-shell smoke tests: trivial command, large-script path, UTF-8 round-trip); verify on the Windows CI runner with and without `__PSLockdownPolicy=4`
- [x] 11.3 Implement `doctor --json` and verify its output parses as a single JSON object containing shells, restrictions, smoke tests and paths

## 12. Packaging and CI

- [x] 12.1 Build `envelope.pyz` with `zipapp` (and an optional wheel for PyPI) and verify `python envelope.pyz --version` and `python envelope.pyz doctor` work from a clean directory with no site-packages
- [ ] 12.2 Set up CI on a platform with Linux, Windows and macOS runners running lint, type checks, unit, integration and protocol tests on Python 3.12, 3.13 and 3.14; verify a green pipeline on all nine combinations
- [ ] 12.3 Add a Windows CI job with CLM forced via `__PSLockdownPolicy=4` running the PowerShell integration tests and `doctor`; verify it passes
- [ ] 12.4 Add interop smoke jobs that run OpenCode and Qwen Code headless against `envelope.pyz` (list tools, call `run` and `output` with a mock or low-cost model); verify both jobs pass

## 13. Acceptance

- [ ] 13.1 Run the full scenario suite derived from all eight capability specs on Linux, macOS and Windows and verify every scenario passes (or is explicitly marked as a documented limitation, e.g. macOS crash safety)
- [x] 13.2 Run `envelope.pyz` on a real corp Windows machine (AppLocker/WDAC, CLM, GPO ExecutionPolicy, Python 3.12) with Qwen Code; verify `doctor` smoke tests pass and a large PowerShell script and a git-bash command succeed via the MCP client
- [x] 13.3 Write the README (installation as `python envelope.pyz`, client registration snippets for OpenCode and Qwen Code, configuration reference, security posture statement that envelope is not a sandbox) and verify all snippets by following them on a clean machine
