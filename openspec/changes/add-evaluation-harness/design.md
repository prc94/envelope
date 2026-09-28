## Context

See proposal.md for motivation. The starting point is `scripts/interop.py`: fixed per-harness projects under `/tmp/envelope-interop`, one shared audit log sliced by byte offset (so trials cannot run concurrently), native shell disabled, three scenarios, one trial per cell. It established the harness pitfalls this design builds on (OpenCode `small_model` and `--dir`; Qwen Code selected via `OPENAI_*` env because it substitutes models for `qwen/…` ids; kill by process group; ≥10 min per run; score from the audit log). Its saved transcripts are in `/tmp` (ephemeral, partly overwritten by an old file-naming bug) and are copied into the repository as test fixtures early.

Findings that shape the design (from exploration and an adversarial review by two independent reviewers):

- **Harness behaviour:** OpenCode's `--format json` is an incremental event stream with per-step tokens and cost. Qwen Code's `-o json` writes one document at exit — **nothing** when the harness is killed (the S5 GLM stall left a 0-byte file); Harbor's Qwen adapter uses chat recording (incremental session JSONL) instead. Qwen Code exposes many non-shell tools by default (`read_file`, `grep_search` with bundled ripgrep, `glob`, `write_file`, `edit`, `web_fetch`, `agent` with sub-agents including `claude-code` and `codex`), and invokes deferred MCP tools through a `tool_call` wrapper. OpenCode has read/grep/glob/edit/write and a title agent that uses a separate small model.
- **OpenRouter streams** carry keep-alive comments (`: OPENROUTER PROCESSING`) and report errors that occur after headers as an in-stream event with HTTP 200.
- **Harbor** (Terminal-Bench's framework) runs real installed agents in containers, but fitting envelope needed about seven workarounds (MCP server config has no `env` field; MCP servers are per task while arms are per job; its Qwen adapter overwrites `~/.qwen/settings.json`; per-trial tokens; host key fallbacks; transcript source; seed plumbing), adds a large dependency tree (including `litellm`) next to the key-holding process, and cannot express the Windows lane. It is **deferred** (E2).
- **Reference tools** reviewed and not adopted: MCPJam (own agent loop; real-harness mode limited to its own models; cloud reporting), MCPLab (non-commercial licence, HTTP-only MCP, no OpenRouter), promptfoo (own loop). Borrowed patterns: `distinct_sequences`, resolved-config snapshots, separating "finished", "passed" and "persisted", retries that replace an attempt, Inspect AI's model-proxy bridge.
- **Envelope today:** git-bash gets **no** code-page fallback (`shells/launch.py`: the git-bash `Launch` has `console_codepage=False`; only cmd and the >8 K PowerShell paths set it), so any non-UTF-8 output in git-bash is replaced by U+FFFD; and the fallback that cmd uses switches a whole stream once (`output/decode.py`), which would mis-decode mixed git-bash streams if simply enabled. `DEFAULT_ENV` has no `GIT_EDITOR`. The head/tail split is the constant `HEAD_SHARE = 1/3` (`output/render.py`), not a setting. The audit log records `arguments` verbatim and its outcome has no truncation or replacement fields.
- **Windows VM** (`docs/windows-test-vm.md`): code page 437 today; full AppLocker (including a script-deny rule for `%TEMP%`, which also puts PowerShell into ConstrainedLanguage), WDAC and GPO enforcement; Qwen Code 0.24.5 installed but that state not yet snapshotted; SSH sessions have no interactive console (the doc asks for console-dependent checks to be repeated interactively); Proxmox-managed by a separate agent; the evaluation licence expires 90 days after install.

## Goals / Non-Goals

**Goals:**
- One runner, one trial lifecycle, one task tree and one results format for every lane.
- Every failure explainable from recorded evidence, with symmetric treatment of envelope and the native shell.
- The provider key confined to one isolated process.
- A first real decision (M1) before breadth (M2).
- New harnesses, API families, models, arms and target operating systems added as data or plug-ins.

**Non-Goals:**
- A general benchmark or leaderboard; LLM-as-judge verdicts.
- Running on the user's corp machine; the VM approximates it.
- CI integration and scheduled runs (later, with P1-remainder's CI decision).
- Giving the runner hypervisor access.

## Decisions

### E1. Scope: defaults and baseline now, tool surfaces in P3a
P2 decides (a) P1 defaults and wordings and (b) envelope versus native shell tools. The A/B/C tool-surface comparison moves to P3a, where `input`/`stop` exist; arms are a first-class axis so P3a only adds arms.

### E2. One host runner with `ssh` and `docker` targets; Harbor deferred
A single runner drives every lane through a target interface (run a command in the trial's containment unit, copy files in and out, list and kill the unit's processes, open tunnels, measure a fingerprint). `ssh` targets are real hosts (Windows VM now, macOS later); `docker` targets start one container per trial from a pinned image, with the network limited to the gateway and test servers, so agents cannot reach host secrets. *Alternatives:* Harbor for Linux plus own runner for Windows — two lifecycles and adapter sets, seven workarounds, a heavy dependency tree next to the key; the Windows VM as a Harbor custom environment (Harbor loads environments by import path) — possible, unverified, and still carries the workarounds. Harbor stays an option for later scale-out of the Linux lane; tasks do not depend on it.

### E3. Tasks: host-side generation, collect on target, judge on host
Tasks live in `eval/tasks/<id>/`: `task.toml` (metadata), `instruction.md`, `generate.py` (runs on the runner host with a per-trial seed; writes fixture files with exact bytes plus an answer key and a manifest), `collect.py` (runs on the target with `python -X utf8`, emits observations JSON), `judge.py` (runs on the host), `oracle` (a scripted solution run through envelope without a model). Generating on the host avoids target-side script restrictions (AppLocker script rules, CLM forbidding the .NET calls needed for CP1251/UTF-16/gzip fixtures, Python defaulting to cp1251 under the Russian locale) and keeps one generator per task for every lane. Fixtures set repository-local git identity and a trial `GIT_CONFIG_GLOBAL`; files are uploaded as the `envelope` user so git's ownership check passes. Per-job generation is allowed for heavy datasets. Uploads use a transfer that preserves bytes; long paths need `LongPathsEnabled` on the VM (task 7.2).

### E4. Harness adapters
One adapter class per harness implements: supported targets; install/version check; user-level config writer (envelope as stdio MCP server with the arm's `ENVELOPE_*` env; the arm's tool set; model provider = gateway URL + trial token); headless argv; transcript location; transcript → normalized trajectory (ATIF-compatible JSON), unwrapping generic wrappers such as Qwen's `tool_call`; harness error detection; and the harness's own report of exposed tools for the tool-set check. **Qwen Code** (M1, `ssh` and `docker`): `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL`, user-level `mcpServers`, tool exclusions, per-trial `HOME`/`USERPROFILE`, incremental transcript via chat recording (or a streaming output mode, whichever the pinned version supports — spike 1.3), exposed tools from its init event. **OpenCode** (M2, `docker` only — not installed on the VM): custom OpenAI-compatible provider pointing at the gateway, `small_model` declared as an auxiliary model, `--dir`, per-trial `XDG_*`, tool toggles, `--format json` stream. Harness versions are pinned per job.

### E5. Model gateway
A standard-library-only Python program (threaded HTTP server, `http.client` upstream, chunk-by-chunk relay); no third-party code shares its process or environment. **Placement:** an unprivileged LXC (`envelope-gateway`, Debian 13) on the lab's Proxmox host, provisioned by `eval/gateway/deploy-lxc.sh`, running as a dedicated user with a 0600 key file. A same-host user, then a Docker container, were considered: on the runner's own host the runner user (a member of `docker`, which is effectively root) could read the key, and the point of isolation is that the runner cannot. The gateway listens on loopback inside the container; the runner reaches it through `ssh -L` (a user systemd unit on the runner host, pinned host key). The runner's key on the container is restricted to `port-forwarding` for the gateway port and to a forced `relay` command that pipes to the admin socket, so it can neither read the key nor run anything else. Admin interface on a Unix socket owned by the gateway user and accessible to the runner's group: issue/revoke tokens (`{lane, job, trial, allowed_models, request_cap}`), query trial state, read a trial's ledger records, run the credential scan. The scan takes a tar stream of the runner's result directory (which is not on the gateway host) and scans it without extracting. The runner gets no Proxmox access; the container is created once by whoever runs the deployment script, which needs root ssh to the Proxmox host. Chat Completions → OpenRouter in P2; other families are route plug-ins. Per request it injects pinned provider routing and usage reporting, relays SSE while timing content deltas and ignoring comment lines, detects in-stream error events, classifies main vs auxiliary requests, and appends one JSONL ledger record per request. Provider guardrails limit which models the key may use (found when the first real request was rejected): the gateway reads `GET /api/v1/models/user`, which the provider filters by the key's guardrails, caches it, refuses tokens for blocked models and records guardrail rejections (`guardrail_blocked`), so a model outside the allowlist is `infra` with a clear reason at the canary gate instead of an opaque 404. Exact OpenRouter field names are confirmed from its documentation before implementation (task 1.4); missing fields are recorded as unknown. *Alternatives:* harness-native provider settings (keys spread to targets, no pinning in Qwen Code); LiteLLM proxy (heavy, supply-chain exposure next to the key).

### E6. Arms and tool sets
An arm is `{name, kind: envelope|native, settings, variant?, tool_set, affects: [tags]}`. Setting-only arms use existing keys (output budget chars/lines, timeouts). Wording and constant arms (descriptions, hints, head/tail split) are **build variants**: a patch in `eval/variants/` applied to a temporary source copy and built with `scripts/build.py`; the variant hash is recorded. The default tool set is shell-only for every arm; tasks that deliberately allow file tools are a separate, explicitly tagged group. *Alternative:* hidden configuration keys for wordings — rejected, product surface for evaluation's sake.

### E7. Containment and leftover processes
Windows: the runner starts the harness through a small Python launcher on the VM that creates a Job Object (reusing envelope's verified ctypes approach: nested jobs work inside the OpenSSH session job, spike S2), starts the harness suspended, assigns and resumes it, waits, then lists processes still in the job (`JobObjectBasicProcessIdList`) as leftovers and terminates the job; on timeout it terminates the job. Envelope's own per-run jobs nest inside it. Linux: the trial's container is the unit; leftovers are listed before the container is removed. Runner processes (tunnels, collect, fingerprint probes) never run inside a unit.

### E8. Console calibration
The corp user runs Qwen Code in an interactive terminal; SSH sessions have no console. Before encoding tasks are trusted, a calibration task captures raw bytes from native tools (`cmd /c dir` on Cyrillic names, `ipconfig`, `net`) and from git-bash running them, both through the SSH launcher and in the interactive session (a scheduled task running as the logged-in `envelope` user with autologon), and compares them. If they differ, Windows trials start through the scheduled-task launcher in the interactive session instead of an SSH child; the launcher interface is the same either way.

### E9. Event timeline and attribution
The timeline merges gateway ledger events, shell tool call start/end (envelope audit `decision` and `tool_call` records in envelope arms; trajectory in the baseline), harness events and exit, and collect/judge results. Attribution follows the evaluation-runner spec: unrecovered upstream failures and setup problems are `infra`; a timeout with a shell tool call in flight, a tool crash, a non-input envelope tool error, or a context-length rejection after a tool result is `tool` (symmetric for envelope and the native shell); an idle harness (no request, no tool call in flight beyond the stall threshold) or a harness error not tied to a tool call is `harness`; runaway loops and rejected malformed calls are `model`; conflicting evidence is `indeterminate`. Silent envelope misbehaviour (mis-decoding, poor truncation, unhelpful hints) is not an attribution — it is measured by lever metrics and known-gap tasks. Stall threshold default: 180 s.

### E10. Lever metrics from transcripts
Truncation, recovery via `output`, hints shown, and replacement characters are derived from the tool result text in the trajectory (header, truncation line, `note:` lines, U+FFFD count) and the audit log's `hints`; hint effect compares the next command with the previous one. Tool discovery counts `tool_search`-style calls before the first shell call and whether the first returned the shell tool. Adding `truncated`/`replaced` to envelope's audit outcome would make this sturdier; that is a candidate P1 follow-up, not part of this change.

### E11. Statistics, decision rules and profiles
Per-cell Wilson intervals are descriptive. Arm comparisons are paired by task (same tasks in both arms, interleaved randomized order) with a bootstrap over tasks for the difference in success rate and in median tokens/turns; continuous metrics carry most of the power. Jobs declare decision rules per lever (non-inferiority margin on success, minimum improvement in tokens or recovery rate); the report evaluates them as met / not met / inconclusive. **Screening** profile: 3 trials per cell, exploratory, no decision rules. **Decision** profile: tasks tagged for the lever, ≥5 trials per task and arm, sized so the bootstrap interval of the success difference is narrower than the declared margin; the report prints the achieved width. Prompt caching is not disabled; cache-read tokens are recorded and interleaving equalises warmth across arms.

### E12. Time budget
Estimated from S5: a trial takes about 1–2 minutes when it succeeds and up to the timeout when it stalls; with setup, collect and SSH overhead, plan for ~3 minutes per Windows trial, serial on one VM (≈20 trials/hour). The M1 decision job is sized to about 150 trials (one night): ~10 `output-budget`/head-tail tasks × 3 arms × 5 trials. A full Windows screening job (~35 tasks × 3 models × 2 arms × 3 trials ≈ 630 trials) is ~30 hours and is run only in M2, possibly split per model. Every job carries a wall-clock cap.

### E13. Windows target lifecycle
The runner gets no hypervisor access. A job declares the snapshot it expects; resetting to it is a manual step (or a request to the Proxmox-operating agent), and the runner verifies the fingerprint (code pages, locale, Qwen version, binary hashes, AppLocker/WDAC policy hashes) before starting. Trials run under `C:\eval\trials\<trial>` (outside `%TEMP%`) as the standard user `envelope`. Tunnels: `ssh -R 127.0.0.1:<port>:127.0.0.1:<port>` for the gateway and each test server, with `ExitOnForwardFailure=yes`, `ServerAliveInterval`, strict host-key checking; health checked before and after each trial. The licence expiry date is recorded in `docs/windows-test-vm.md` with the rebuild procedure; jobs refuse to start within 3 days of expiry.

### E14. Database servers
MongoDB and Redis run as containers on the Linux host bound to loopback and the Docker bridge; Windows reaches them through reverse tunnels. Per trial the generator creates a Redis ACL user limited to its random key prefix (with dangerous commands removed) and a Mongo user with `readWrite` on its random database, writes connection details to a workspace file, and cleanup deletes both; at job start and end, objects with the job's prefix left by interrupted trials are removed.

### E15. Secrets
The provider key exists only in the gateway (E5). Gateway tokens and database users are per trial and revoked or deleted when the trial ends; they are registered as trial secrets and redacted from generated reports. The credential scan runs over each job's result directory. Before the first Windows job, the VM and its snapshots are checked for an OpenRouter key stored by the task 13.2 Qwen Code tests (settings files, environment); if found, the key is rotated.

### E16. Code layout and milestones
`eval/` is a `uv` project (`eval/pyproject.toml`) with `src/envelope_eval/` (`runner/`, `targets/ssh.py`, `targets/docker.py`, `adapters/`, `tasks/`, `report/`), `eval/gateway/` (standalone, standard library only, no project dependencies), `eval/launcher/` (the Windows Job Object launcher, standard library only), `eval/tasks/`, `eval/variants/`, `eval/jobs/*.toml`, `eval/tests/` (including S5 transcripts as fixtures) and git-ignored `eval/results/`. **M1**: gateway, runner with `ssh` target, Qwen Code adapter, task framework, ~15 tasks (canaries, the three interop scenarios, `output-budget`/head-tail tasks, the two known gaps, calibration), VM preparation, report, and the output-budget/head-tail decision job. **M2**: `docker` target and Linux lane, OpenCode adapter, native baseline arm, full Windows and Linux suites, database tasks, screening jobs, baseline report, retirement of `scripts/interop.py`. If M2 grows, it moves into its own change.

### E17. macOS later
A macOS lane is an `ssh` target on an ephemeral Tart VM clone per job (or a hosted macOS runner), with a macOS containment unit (process group plus the kqueue watchdog from P1-remainder), the gateway through a reverse tunnel, and macOS-specific tasks (zsh default, BSD tool differences, quarantine, TCC-protected directories).

## Risks / Trade-offs

- [Qwen Code configuration cannot disable some built-in tools or changes between versions] → Spike 1.3 at the pinned version; the per-trial tool-set check turns any drift into `infra` rather than contaminated results.
- [Console calibration shows SSH differs from interactive sessions] → Scheduled-task launcher in the interactive session (E8); autologon on the VM is an approved VM change.
- [One serial VM makes jobs slow] → M1 decision job sized to one night; screening only in M2; per-model splitting; wall-clock caps.
- [VM drift or expiry] → Fingerprint check before every job; expiry recorded, rebuild procedure documented, jobs refuse near expiry.
- [Attribution rules misclassify] → `indeterminate` instead of forced guesses; timelines kept with each trial for audit; unit tests per rule.
- [Small samples mislead] → Paired task-level comparisons, declared decision rules, exploratory label on screening, achieved interval width always printed.
- [Checker bugs masquerade as model failures] → Oracles first on every target; checker crashes are `infra`.
- [Gateway single point of failure] → Canary gate and circuit breaker mark cells unavailable instead of failing them.
- [Native baseline on Windows runs in a different shell than git-bash] → Only goal-stated tasks are baseline-eligible there; "the agent works in your shell" is reported as a separate claim with its own tasks.
- [Transcript-derived lever metrics are fragile] → Parser tests on real transcripts, including killed runs; candidate P1 audit fields as follow-up.
- [Stdlib-only gateway is more code than a library-based proxy] → Scope limited to one API family in P2; fake-upstream tests for streaming, keep-alives and in-stream errors.

## Migration Plan

Additive. `scripts/interop.py` stays until the interop tasks run on the Linux lane in M2 with verdicts comparable to S5, then it is removed together with its README/CLAUDE.md references. Rollback: delete `eval/` and the gateway user; envelope is unaffected.

## Open Questions

- Which Windows `redis-cli` build the corp machine uses (affects only which binary is installed on the VM in M2).
- Whether a fast, harness-free inner loop for wording iterations (own mini agent loop or MCPJam's SDK) is worth adding later as another lane.

## Spike notes (2026-09-25)

The OpenRouter API documentation confirms that streamed Chat Completions requests
request usage with `stream_options: {"include_usage": true}`. Provider routing is
the `provider` object; its `order` list selects providers and `allow_fallbacks: false`
prevents silent provider substitution. Generation metadata is available from
`GET /api/v1/generation?id=...`, including `provider_name`, `model`, `tokens_prompt`,
`tokens_completion`, `native_tokens_reasoning`, `total_cost`, and `streamed`.
The gateway therefore records unknown usage when the terminal stream event omits it
and records served model/provider fields when present. OpenRouter streams may also
emit keep-alive comment lines (`: OPENROUTER PROCESSING`); those bytes do not update
content timing. An error event after HTTP 200 is treated as a failed request.

References: OpenRouter Chat API (`stream_options` and `provider`), provider routing
guide, and generation lookup API (all checked 2026-09-25).

### Qwen Code 0.24.5 VM probe

The Windows VM at `admin@192.168.94.172` reports Qwen Code `0.24.5`, Python
3.12.10, Windows 10 Enterprise LTSC build 19044, and no configured MCP servers.
The CLI supports incremental `--output-format stream-json` (with
`--include-partial-messages`) and disk chat recording via `--chat-recording`.
The relevant headless controls are `--mcp-config`, `--allowed-tools`,
`--exclude-tools`, `--max-tool-calls`, `--max-subagent-depth`, and `--model`.
The adapter will use stream-json for a kill-tolerant trajectory, an explicit MCP
config, and both allow/exclude controls; a tool-set check will consume the init
event from that stream. A throwaway run against a temporary fake OpenAI-compatible
SSE server succeeded with `--auth-type openai`, producing a successful one-turn
`stream-json` result. With the temporary envelope MCP config, Qwen reported
`mcp__envelope__run` and `mcp__envelope__output` in its init event. A second probe
that attempted to keep the fake server detached was terminated by the VM policy,
so the tool-call round trip remains unverified. The VM's current code page is 437 and locale 0409, so
the Russian locale calibration remains a separate approved VM task.

### Spike 1.3 findings: Qwen Code (2026-09-25)

Throwaway runs against a fake OpenAI-compatible server that logged the `tools` array of every
request. The bare/non-bare/exclusion probes were run on **0.24.5 on the VM** (through a reverse
tunnel) and on 0.24.4 on Linux (the version installed locally; 0.24.5 was not installed there)
with identical results; the shell and exit-code findings are from the VM.

- **Incremental transcript:** `-o stream-json` writes one JSON object per line as events happen.
  After a SIGKILL at 8 s the file held the `system/init` event and the in-flight `tool_use` with no
  partial line, so it is the kill-tolerant transcript; `-o json` is not (S5). `--chat-recording`
  additionally writes `~/.qwen/projects/<cwd-slug>/chats/<session>.jsonl` and is not needed.
  Events: `system/init` (`tools`, `mcp_servers`, `model`), `assistant` and `user` messages with
  content blocks (`tool_use {id,name,input}`, `tool_result {tool_use_id,is_error,content}`,
  `text`), `stream_event` (only with `--include-partial-messages`; can be dropped) and a final
  `result` (`subtype`, `is_error`, `num_turns`, `usage`). Blocks are nested under `message.content`,
  so the current `parse_transcript` (flat events) must be extended in 6.2.
- **Exit code is unreliable on Windows over SSH:** a run with a `result: success` event exited 9
  (libuv assertion `UV_HANDLE_CLOSING` at teardown). Score from the `result` event, not the exit
  code. `--max-wall-time` and `--max-session-turns`/`--max-tool-calls` exist and abort with exit 55.
- **What the model sees is not what init reports.** Default init lists 28 tools, but the first
  request offers 14 (the rest are deferred behind `tool_search`/`tool_call`) and later turns
  differ. The ground truth is the request's tool names, now recorded by the gateway as
  `tool_names`; the tool-set check must compare those to the expected model-visible set, with
  init as a secondary check that nothing extra is configured.
- **Disabling tools:** `--exclude-tools a b c…` removes each named tool from both init and requests
  (unknown names are ignored). `--core-tools run_shell_command` is not an allowlist (agent, skill,
  `tool_search`, `tool_call`, goal and worktree tools stay). Excluding every built-in in the
  default init except `tool_search`/`tool_call` (and `run_shell_command` in the native arm) leaves
  exactly those in the request. `agent` and `list_agents`/`send_message` are excludable.
  `--bare` starts with only six built-ins (`read_file`, `notebook_edit`, `run_shell_command`,
  `get_goal`, `update_goal`, `edit`); with `--mcp-config` and those excluded the model sees exactly
  `mcp__envelope__run` and `mcp__envelope__output`.
- **Deferred MCP calls:** outside `--bare`, MCP tools are hidden: the model sees `tool_search` and
  `tool_call {name, arguments}` and calls `mcp__envelope__run` as
  `tool_use name="tool_call" input={name:"mcp__envelope__run", arguments:{…}}`; the result is the
  MCP text verbatim. Adapter unwrapping maps this to the inner tool. `--bare` exposes MCP tools
  directly, which removes the discovery step the tool-discovery metric (E10) measures. **Decision
  for 6.2:** use non-bare (a corp user runs the interactive, non-bare client) with a full
  exclusion list; the existing adapter's `--bare` contradicts E10 and is to be dropped.
- **Hidden auxiliary agent:** outside `--bare`, after the first turn Qwen makes a second request
  from a background managed-memory agent, carrying `edit`, `write_file` and `run_shell_command`
  regardless of `--exclude-tools`. It would count as a main request and could run shell commands.
  Settings `{"memory": {"enableManagedAutoMemory": false, "enableManagedAutoDream": false}}` remove
  it (the same keys at the top level do not). Adapter settings must include them.
- **Native shell on Windows:** `run_shell_command` runs **cmd.exe**: `%COMSPEC%` and `%OS%` expanded,
  `$0`, `$BASH_VERSION` and `$PSVersionTable` did not, and `bash` is not on the VM's PATH, so the
  native baseline on Windows is a cmd shell, not git-bash (as E15's risk table already assumes).

### Provider guardrail and gateway placement findings (2026-09-25)

- The OpenRouter key is restricted by a workspace guardrail. `GET /api/v1/models/user` through the
  gateway lists exactly six ids, three models with a dated alias each: `deepseek/deepseek-v4-flash`
  (`-0731`, `-20260731`), `qwen/qwen3.8-flash` (`-20260826`) and `z-ai/glm-5.3-flash`
  (`-20260826`). The `:free` slugs and `openai/gpt-oss-120b` are excluded (HTTP 404 with
  `model-ignored-by-guardrail`). These three are the M1 candidate models (task 9.4) unless the
  guardrail changes; the gateway re-reads the list and `issue` refuses anything else.
- First real request (task 2.6): `qwen/qwen3.8-flash` through the tunnel, LXC gateway, OpenRouter:
  HTTP 200 in about 1.1 s, provider Alibaba, served model equal to the requested one, cost
  about $0.00002 for 68 prompt and 20 completion tokens. It is a reasoning model: with
  `max_tokens: 20` the reply had `content: null` and a `reasoning` field, `finish_reason: length`
  and 20 reasoning tokens. The ledger's `completion` is empty in that case (it records content,
  not reasoning), and the Qwen adapter and any token cap must allow for reasoning output.
- Verified for the LXC placement: the runner's restricted account can only run the relay and forward
  the gateway port (a `cat` of the key file and a forward to port 22 were both refused), the key
  exists only inside the container, and issue/state/ledger/revoke work through `ssh … relay`.

### Launcher verification on the VM (task 5.2, 2026-09-25)

`eval/launcher/verify.py` (stand-in harnesses: one leaves a background child, one hangs with a child
and a grandchild, one exits with a code, one writes UTF-8) passes on Windows 10 build 19044 as the
standard user `envelope`, over SSH, and on Linux with the launcher's POSIX stand-in. Findings: nested
Job Objects work inside the OpenSSH session job; the timeout kills the whole tree (checked with
Toolhelp32); a process outside the unit (standing in for the SSH tunnel) is untouched; and
`conhost.exe` is a job member for about 0.1 s after a console harness exits, so it is excluded from
the leftover list (as are members that vanish between the job query and the process snapshot).

### SSH target verification on the VM (task 5.1, 2026-09-25)

Measured live as the standard user over SSH: the VM's fingerprint (Windows-10-10.0.19044-SP0, ACP 1252,
OEMCP 437, locale 0409, Python 3.12.10, Qwen 0.24.5, hashes of python/git/qwen, the WDAC policy file and
the AppLocker rule set) by streaming `launcher/fingerprint.py` to `python -` (no upload, so no script
rules apply). A job declaring `oemcp = 866` was refused (`oemcp: declared 866, measured '437'`); one
declaring the real values passed. A reverse tunnel bound to 127.0.0.1 on the VM reached the gateway in the
LXC from the VM (HTTP 401 from the gateway) and nothing listened on 0.0.0.0. Killing the tunnel's ssh was
detected by the health check, the job paused about 3 s while `restore_tunnels` reopened it, and the trial
ran afterwards. Note: `cmd` mangles multi-line `python -c` arguments, so code is sent on stdin.

### Per-trial isolation and the Qwen Code adapter, verified on the VM (tasks 5.3 and 6.2, 2026-09-26)

- **Isolation (5.3):** `WindowsTrialTarget` gives each trial `C:\eval\trials\<trial>` with its own workspace,
  `HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`, `GIT_CONFIG_GLOBAL` and envelope's `ENVELOPE_AUDIT_LOG` and
  `ENVELOPE_SPOOL_DIR`; a stale directory of the same name is removed first, and the whole tree at the end.
  `C:\eval` is created by the runner as the standard user (the ACL on `C:\` allows it), so it is not a task 7.2
  step. `GIT_CONFIG_NOSYSTEM` is deliberately not set: the system gitconfig (for example `core.autocrlf`) is
  part of the environment being tested. An oracle stand-in harness (`launcher/oracle.py`, `OracleAdapter`)
  runs a task's oracle through envelope's MCP server in the containment unit and writes a Qwen-shaped
  transcript, so the whole trial pipeline runs without a model; the canary passed twice on the VM this way,
  and a planted key (including a Cyrillic one) was found by the confinement search, which reports files only.
  Generators declare secrets with `{"secrets": [...]}` in their answer key; a hit before the agent starts is
  `infra`.
- **Adapter (6.2):** real Qwen Code 0.24.5 on the VM, started through `node cli-entry.js` (the `qwen.cmd` shim
  only does that, and `cmd` would mangle a multi-line prompt), non-bare, with `--exclude-tools` for every
  built-in the arm does not list, `memory.*` off, envelope as a user-level MCP server whose environment carries
  the trial's directories and the arm's settings. With a fake model behind the gateway the trial passed: the
  model was offered exactly `tool_call` and `tool_search` on every request, its `tool_call` wrapper was unwrapped to
  envelope's `run`, and the audit log gave the tool events. The executor now also compares the tool names the
  gateway saw in each main request with the adapter's `visible_tools` (a hidden agent or a re-enabled tool makes
  the trial `infra`), and the adapter reports `implicit_tools` (the deferral wrappers).
- **Real canary:** `qwen/qwen3.8-flash` through the tunnel and the LXC gateway passed in 13 s (Alibaba, 6,075
  prompt and 25 completion tokens of which 20 reasoning, $0.0009). Qwen Code's own system prompt is about 6,000
  tokens, which dominates cost per turn. The model answered without calling a tool, which the canary's judge
  accepts (it checks the route, not envelope); the trial record shows `shell_calls: 0`. The canary timeout was
  raised from 30 s to 120 s for reasoning models.

### M1 tasks and known-gap tasks verified (tasks 8.2 to 8.4, 2026-09-26)

- **Tasks:** `canary`, the three interop scenarios (`interop-basic`, `interop-truncation` with an `output` step,
  `interop-long` that waits 40 s), eight `output-budget`/head-tail tasks (`git-log-ticket`, `logs-trace-id`,
  `logs-last-traceback`, `logs-error-window`, `src-grep-nonce`, `build-progress-noise`, `build-log-first-error`,
  `csv-middle-row`) and the two known-gap tasks (`git-merge-editor` for `git-editor-default`, `gitbash-cp866-dir`
  for `gitbash-no-codepage-fallback`, Windows only). All 14 oracles pass on the VM (git-bash through envelope)
  and on this host. Expected values differ per trial (`<<name>>` placeholders filled from the answer key's
  `params`), so answers cannot be memorized; mechanics tasks read envelope's audit log in `collect`.
- **Findings from building them:** (1) `scp -r` is not trustworthy on the Windows VM (a directory of plain files
  landed flat in the parent, and it cannot carry non-ASCII names safely), so trees now travel as a tar stream that
  Python on the target extracts; single files still use scp. (2) `cmd`'s `dir` at OEM 437 replaces Cyrillic with
  literal `?` (information lost, not a decoding problem), and `chcp` inside the same `cmd` line has no effect (the
  S1 finding); the CP866 gap itself needs the Russian OEM code page of task 7.2 to show. The task's oracle tries
  the native `dir`, then lists with git-bash's `ls`, which works at both code pages. (3) The oracle answer is the
  tool's body without envelope's `exit N | ...` header, as a model's answer would be.
- **Real trials under gap ids:** Qwen Code with `qwen/qwen3.8-flash` passed both gap tasks on the VM (38 s and 26 s,
  about $0.001 each; at OEM 437 the CP866 gap does not occur and the model found the file via `tool_search` and
  `run`); the report lists them under `git-editor-default` and `gitbash-no-codepage-fallback` with pass rates,
  apart from the other results.
- **Report:** cells now include the model (a cell is lane x task x harness x model x arm), known-gap results are
  their own section, and broken or unavailable trials are listed compactly. Paired comparisons still pool models;
  the M1 decision job needs per-model comparisons (rule filter by model) before task 9.4.

### Provider rate limits seen by the smoke job (2026-09-26)

The first live run of the whole pipeline (`eval/jobs/smoke.toml`: three tasks, one model, three arms including the
head/tail build variant, gates with real oracles, the post-job credential scan through the relay) exercised every
component and turned up two things. (1) `qwen/qwen3.8-flash` was rate-limited upstream (HTTP 429, "provider shared
pool"): one canary probe failed with 16 requests and 0 tokens, the next passed after 15 requests, and a plain
`interop-basic` trial took 139 s. Qwen Code retries with its own long backoff, so a flaky provider looks like a slow,
noisy trial. The gateway now absorbs transient errors itself (see the gateway README and the spec requirement), and the
runner counts only completed main requests as turns and tokens (`failed_requests` and `absorbed_retries` are recorded
separately); before the fix a trial with three real turns reported 19. (2) The canary gate did its job: the all-429
probe was `infra`, the cell stayed available because the next probe passed.

### Native-shell arm and the retrying gateway, verified live (2026-09-26)

A native arm trial (`kind = "native"`, tool set `run_shell_command`, no MCP server) of `csv-middle-row` on the VM
passed: the trajectory shows the native `run_shell_command` call, there is no envelope audit log, the timeline's
tool events come from the trajectory, and the gateway's retries absorbed 4 rate limits (0 failed requests, 2 real
turns, 58 s against about 150 s before). The Qwen half of task 11.1 is done; the OpenCode half waits for 10.3.

### E8 result: console calibration (task 8.1, 2026-09-26)

Method: `python -m envelope_eval calibrate` (`eval/launcher/calibrate.py`, `envelope_eval/calibration.py`) runs one script
through the same launcher in two contexts and compares the raw bytes: from an SSH session (session 0, what trials
use) and from a scheduled task in the interactive session of the logged-on `envelope` user (session 1, via temporary
autologon with a fresh random password; autologon and the password value were removed afterwards, the session logged
off, and the VM's fingerprint digest re-checked against the P2 record). Thirteen commands, each started the way
envelope starts children (no console window): `dir /b` on a directory with Cyrillic names through cmd, PowerShell
and git-bash, `echo` of Cyrillic text, `chcp`, `ipconfig`, `net help`, `net user`, the PowerShell console encodings and
git-bash's own `ls`/`echo`.

- **Twelve of thirteen are byte-identical**, and the console facts agree in both contexts: ACP 1251, OEMCP 866, console
  input/output code page 866/866, and no console window in either (the launcher's `CREATE_NO_WINDOW`). So everything
  that matters for the encoding tasks (native tool output in OEM 866, the ANSI/OEM split, PowerShell's encodings) is
  the same whether or not a console session exists.
- **One difference, not about encoding:** `net user` exits 1 and prints `\\` (an empty machine name) in the SSH
  session, and exits 0 with `\\WIN10-PLYGRND` interactively. The SSH session cannot resolve the local computer name
  for that call. Trials that use commands relying on the window station, the desktop or the machine-name lookup can
  behave differently from an interactive terminal.
- **Decision:** the SSH launcher stays. E8's rule was to switch to the interactive launcher if the raw bytes differ, and
  they do not; the one difference is behavioural, and switching would mean keeping autologon (the password in the
  registry, readable by the standard user a Qwen trial runs as) on for the whole evaluation. The limitation is recorded
  here so a later result that depends on such a command is read with it in mind; re-running the calibration
  (autologon is a two-value registry change plus a reboot) is cheap if that ever matters. The canary was not
  re-run because nothing was switched.

### First decision-job attempt: a cell lost to a transient rate limit (2026-09-26)

The first launch of the M1 job lost `qwen/qwen3.8-flash` entirely. The provider's shared pool rate-limited it at the
start, the gateway retried for about 53 s and gave up, the 120 s canary timed out twice, both probes were `infra`, and the
breaker then skipped all of that model's trials for the whole 12-hour job (6 of 525 trials ran before I stopped it). The
gates did what they were designed to do; the design was wrong to make the loss permanent. Changes: a tripped breaker is
half-open after a cool-down (15 min) and the canary gate probes a failed cell again; trials of an unavailable cell are
parked, not written, retried every minute for up to an hour after the plan (and whenever the cell recovers, as the
interleaved order reaches them again), and recorded as unavailable once, at the end, only if they never ran; the gateway waits
longer (8 retries, 45 s cap, 150 s budget); the canary's timeout is 300 s. The aborted attempt is kept under
`eval/results/m1-decision-20260926-032037-aborted-rate-limit`.

### OpenCode adapter (task 10.3, code and parser only, 2026-09-26)

`OpenCodeAdapter` writes a per-trial user config (custom `@ai-sdk/openai-compatible` provider at the gateway with the
token referenced as `{env:EVAL_GATEWAY_TOKEN}` so it never lands in a file, `model` and `small_model` both the model under
test, every built-in tool off except the arm's, `permission` deny for bash/edit/webfetch, envelope as a local MCP server
with the trial's environment), runs `opencode run --pure --format json --dir <workspace> -m gateway/<model>`, and parses
the event stream (`tool_use` parts carry call and result together) into the same trajectory as Qwen's; its tests use the
real S5 transcripts. OpenCode reports no tool list at start-up, so `reports_tools = False` and the tool-set check rests on
the tool names the gateway saw. Not yet exercised against a real OpenCode: that needs the Linux image (10.1).

### Early M1 observation: malformed deferred tool calls (2026-09-26, 46 trials in)

About 60% of the first 46 M1 trials failed, 16 of them DeepSeek trials the run had labelled `indeterminate`. The ledger shows the model
often answered correctly mid-trial, then issued a `tool_call` (Qwen's deferred-tool wrapper) whose JSON arguments the provider
(Sail Research) rejected with a 502, `invalid arguments for tool_call`; Qwen retried the same history three times and the harness
failed. That is a model-side malformed call, not infrastructure, so attribution now recognises it (`malformed_call`, tested); the
running job predates the change, so its records keep the `indeterminate` label (pass/fail, and therefore every comparison, is
unaffected). Consequence for the M1 reading: the deferred wrapper costs models with weak tool-call JSON a large share of trials
regardless of the envelope arm, which lowers the power of the budget comparisons for that model.

**Decision (2026-09-26, user):** Qwen Code stays non-bare in every job, despite the malformed deferred-tool-call failures above: results
must describe the setup users actually run, and `--bare` would remove the tool-discovery step E10 measures. Instead, reports show the
malformed-call rate per model beside each comparison. A `--bare` run of the budget lever alone is the fallback if a model's malformed
rate makes its comparison unreadable; it is not planned.

### M1 decision job result (task 9.4, 2026-09-26)

Windows lane (Win10 19044, OEM 866), Qwen Code 0.24.5 non-bare, envelope arms `default`, `budget-small` (6,000 chars / 200 lines),
`budget-large` (24,000 / 800) and `headtail-half` (1/2 head instead of 1/3), on the ten `output-budget` tasks, five trials per cell.
Reports: `eval/results/m1-merged/report` (Qwen and GLM from `m1-decision-20260926-033610`, DeepSeek from
`m1-deepseek-pinned-20260926-185715`). Rules are evaluated per (model, task) stratum with a bootstrap over tasks.

- **Coverage:** 231 Qwen/GLM trials (the first job was stopped at 343 of 525; DeepSeek was replaced) plus 175 DeepSeek trials. Qwen 113 of about 175 planned,
  GLM 118; `headtail-half` has 17 to 25 trials per model instead of 25.
- **DeepSeek needed a provider pin.** Unpinned it passed 1 of 45: the provider Sail Research rejected its deferred `tool_call` JSON (502 `invalid arguments`),
  which is a provider effect, not the budget. Pinned to DeepInfra it passed 174 of 175. The gateway pin (`/etc/envelope-gateway/pins.json`) is now part of the setup.
- **`adopt-budget-small`: not met.** Success is non-inferior (+0.025, interval -0.047 to +0.105, margin 0.05), but tokens fall only 2.7% (interval -5.7% to +10.4%) against the
  required 15%: even the best case is below it.
- **`adopt-budget-large`: not met.** Success is fine (+0.041) but tokens rise 7.3% (interval -15.3% to +1.3%).
- **`adopt-headtail-half`: inconclusive.** Success is fine; tokens change by -3.7% (interval -10.6% to +2.3%) around the 0 margin, with only 15 strata.

**Recommendation: keep the shipped defaults** (12,000 chars, 400 lines, 1/3 head). Neither budget buys a meaningful token saving, and success is at a ceiling for
DeepSeek and Qwen (over 95%), so success cannot discriminate between arms on these tasks; only token cost can. The head/tail split is not shown to matter; more
`head-tail` tasks or trials would be needed to settle it, and nothing here argues for changing it. GLM 5.3 flash is the weak model (9 of 41 early, mostly model failures), so its
cells carry most of the variance.

### Correction to the M1 result: the provider decides more than the arm (2026-09-26)

Counting trials by serving provider from the gateway ledger shows the unpinned GLM numbers above were a provider effect, not a
model one: OpenRouter routed 535 of 649 GLM requests to OpenInference (10 of 79 trials passed) and GMICloud passed 15 of 15. DeepSeek
showed the same at its extremes (Sail Research 0/82, Relace 23/24, DeepInfra 174/175); Qwen only ever used Alibaba (103/113). Consequences:
(1) the "GLM 5.3 is the weak model" sentence in the M1 result is withdrawn; (2) all three models are now pinned in the gateway
(`/etc/envelope-gateway/pins.json`: DeepSeek to DeepInfra, GLM to GMICloud, Qwen to Alibaba); (3) trial records and the report list the
serving providers and flag any model served by more than one; (4) GLM was rerun pinned (`eval/jobs/m1-glm-pinned.toml`) and the M1 rules are to be
recomputed with all three models pinned before the recommendation is treated as final. The DeepSeek and Qwen results are unaffected (pinned or single-provider).

### M1 result, recomputed with all three models pinned (2026-09-27)

GLM pinned to GMICloud passed 166 of 175 trials (default 49/50, budget-small 47/50, budget-large 46/50, headtail-half 24/25): the provider, not GLM, was the
weak link. Merged report `eval/results/m1-merged/report` (Qwen 113 trials from the first job, DeepSeek 175 pinned to DeepInfra, GLM 175 pinned to GMICloud; 30 strata for
the budget rules, 15 for head/tail). The verdicts do not change and the earlier "provisional" flag is lifted for the budget rules:

- `adopt-budget-small`: **not met**. Success interval -5.4 to +5.9 points (inconclusive against the 5-point margin), tokens -1.8% (interval -10.1% to +5.9%): no saving anywhere near 15%.
- `adopt-budget-large`: **not met**. Success is non-inferior; tokens rise 9.3% (interval +1.8% to +16.4%): it costs more.
- `adopt-headtail-half`: **inconclusive**. Success fine; tokens -5.0% (interval -11.2% to +0.4%), interval straddling the 0 margin.

**Recommendation stands: keep the shipped defaults** (12,000 chars, 400 lines, 1/3 head). Success is 95% or better for every model, so it cannot separate arms; only tokens do, and neither
budget saves them. The head/tail split shows a possible small token saving with a half head (-5%, not significant); it would take more `head-tail` tasks to settle, and the effect is too small to justify a change on this evidence.
Known limits: Qwen has 113 of about 175 planned trials; the runs are Windows/Qwen Code only.

### Linux lane host: envelope-linux LXC on mercury (2026-09-28)

Sibling of `envelope-gateway` (114): container 115, `envelope-linux`, unprivileged, `nesting=1,keyctl=1`,
Debian 13, 4 cores / 8 GB / 40 GB, DHCP (192.168.94.41). SSH key at `~/.config/envelope-eval/linux_ed25519`,
standard user `envelope`, no root or password login. Docker 26.1.5 (`docker.io` from Debian's own repo) runs
with the default `overlay2` storage driver inside the unprivileged container without any extra host
configuration — no `vfs` fallback was needed. `envelope` is in the `docker` group, so trial runs need no root.

Pulled: `mongo:8`, `redis:8` (task 12.1's servers). Built: `envelope-eval-linux:2026-09-28` from
`eval/docker/Dockerfile` — Python 3.12.14, bash/zsh/fish/sh, git 2.47.3, Node.js 22, Qwen Code 0.24.5 (matches
the Windows VM's pin) and OpenCode 1.18.32 (the version already in local use here; the S5 fixtures don't record
which version produced them). A non-root `trial` user is baked in; envelope.pyz itself is not, since it is
uploaded per job like the Windows variants. Image is 1.02 GB.

Still to do (10.3, 12.1/12.2): the OpenCode adapter's live verification, and wiring MongoDB/Redis into job
runtime with per-trial ACLs. The `docker` target itself is implemented and verified — see the entry below.

### `docker` target implemented and verified live (tasks 10.1, 10.2, 2026-09-28)

`LinuxTrialTarget` (`eval/src/envelope_eval/targets/linux.py`): one container per trial on `envelope-linux` (192.168.94.41),
reached over SSH exactly like the Windows target. Workspace paths are `container:path`, the same shape `docker cp`
itself uses, so which container a path belongs to travels with the string and one target instance can serve
concurrent trials with no instance state. Every trial container joins a Docker network created with `internal:
true`: no route beyond the Docker host at all, confirmed live (a container could not reach the internet, nor
mercury, nor any address outside its own host). The model gateway and, later, MongoDB/Redis are reached through
the Docker bridge's gateway address on that host, not loopback (a container cannot reach the Linux host's own
127.0.0.1). Leftover processes: `launch.py` runs inside the container via `docker exec`, giving the same
session-based leftover detection as the Windows and local targets; `cleanup` additionally runs `docker top` before
force-removing the container, as the task asked. Fingerprint is the image digest plus the Docker server version.

Three bugs found only by running this live, none caught by the unit tests written first:
1. **`SSHTarget.open_reverse_tunnel`/`probe_port` only ever checked `127.0.0.1`** regardless of the requested
   `bind` address, and OpenSSH's default `GatewayPorts no` silently forces a `-R` reverse forward onto loopback
   even when a different bind address is requested — so the tunnel looked "up" while actually being unreachable
   from inside a container. Fixed: `probe_port` takes the bind address and every caller passes it;
   `GatewayPorts clientspecified` is now set on `envelope-linux`'s sshd (allows exactly the address the client's
   `-R` names, not a wildcard). The bind check itself was already relaxed to loopback-or-private, matching the
   gateway's own `validate_bind_address` and the spec text for Docker targets that predates this work.
2. **`docker cp` nests a directory source under an existing destination directory** instead of merging into it —
   the same class of bug as `scp -r` on Windows. `upload()` now sends a trailing `/.` for directory sources.
3. **The "file not found" wording from `docker cp` is not what was checked for**: it says `Could not find the
   file ... in container ...`, not `No such container:path`; the substring check now recognises both, so a model
   that answers without calling a tool (no audit log ever written) is `passed`, not `infra`.

Live results: a fake-model canary trial and a real one (`qwen/qwen3.8-flash`, through the LXC gateway and the
Alibaba pin, $0.001, no leftovers) both pass; `envelope doctor` inside the image lists bash/sh/zsh/fish, all
passing their smoke tests; a trial container has no route to mercury or the gateway host and no access to the
Linux host's own SSH keys. `rg` (ripgrep) shipped without its executable bit from the Qwen Code npm package;
the Dockerfile now `chmod +x`s it after install.

### OpenCode adapter live verification (task 10.3, 2026-09-29)

A fake-model canary trial and a real one (`qwen/qwen3.8-flash`, Alibaba pin) both pass on the `docker` target
against `envelope-eval-linux:2026-09-28`, run directly through `TrialExecutor` (no permanent job/CLI wiring for
non-Windows targets exists yet — `runner/wiring.py` still only builds Windows `ssh` targets, so this and 10.2's
live verification were both throwaway scripts, not committed). The fake-model gateway ran locally on this host
(`gateway.py --upstream 127.0.0.1:<fake port> --plain-upstream`, `FakeModel` from the test suite extended with a
plan that calls `envelope_run` when the request offers it) and was reached from the trial container through the
same reverse-tunnel chain the real gateway uses; the real trial reused the existing `client_from_config()` admin
path and the already-running `envelope-gateway-tunnel` local forward.

One real bug, not caught by the unit tests (which never build an actual command line against a `docker` target):
`TrialExecutor._run` passed `adapter.command()` the raw `Workspace.workspace` string, which for `LinuxTrialTarget`
is the `container:path` addressing form `upload`/`download`/`docker cp` need, not a path that exists inside the
container. `OpenCodeAdapter.command()` puts it on the command line as `--dir`, so OpenCode failed immediately
("Failed to change directory to envelope-eval-...-c00001:/home/trial/workspace") — invisible to the unit tests,
which use `LocalTrialTarget`/`WindowsTrialTarget` or fakes, where `workspace` is already a plain path. Fixed with
a `UnitTarget.real_path()` method (identity for `local`/`windows`, strips the container prefix for `linux`);
`trial.py` now resolves `ws.workspace` through it before calling `adapter.command()`. `QwenAdapter.command()`
ignores its `workdir` argument entirely, which is why the Qwen live runs on `docker` (10.2) never hit this.

Second finding, environmental rather than code: the trial containers join the `envelope-eval` Docker network
(`docker network create --internal`), not the default `docker0` bridge, so its gateway address is not the
commonly-quoted `172.17.0.1` — on `envelope-linux` it is `172.18.0.1` (`docker network inspect envelope-eval`).
Using `172.17.0.1` for the gateway URL (as the adapter's own unit-test fixture does, correctly, since that
value is just a fixture literal) gives "Network is unreachable" from inside a trial container. Whatever job
tooling eventually wires the `docker` target for real (still open: `runner/wiring.py`) must read the network's
actual gateway address rather than assuming the default bridge's.

### Native-shell arm, OpenCode half verified live (task 11.1, 2026-09-29)

The OpenCode half of 11.1 was waiting on 10.3; with the adapter's `real_path()` fix in place, a native-arm
(`tool_set = ("bash",)`) trial of `csv-middle-row` on the `docker` target passed real, non-fake
(`qwen/qwen3.8-flash`): the trajectory shows one `bash` tool call (`shell_calls: 1`, `tool_sequence: ["bash"]`),
the config OpenCode wrote has no `mcp` key, and `/home/trial/state/audit.jsonl` was never created inside the
container (checked before the trial container was removed) — envelope never ran, matching the Qwen native-arm
result already recorded above. `canary` itself does not force a tool call (the model may just answer, as the
real-canary entries above note), so, like the Qwen verification, this used `csv-middle-row`, which requires
reading a generated fixture.

### Database servers: `DatabaseAdmin`, per-trial ACL users and prefix GC (task 12.1, 2026-09-29)

`runner/databases.py`: long-lived `mongo:8`/`redis:8` containers on the same `docker` host as the trial
containers, joined to the trial network (`envelope-eval`) so a trial container reaches them by container name
through Docker's embedded DNS, and also published on the host's own loopback for the runner's admin connection
and the Windows lane's reverse tunnels later (12.2). Admin operations (`redis_user`/`mongo_user`/`*_delete`/
`*_gc`) run over `docker exec redis-cli`/`mongosh` through the same SSH connection that drives the `docker`
lane's trial containers — never through a trial's own network. A Redis user is scoped with
`~<prefix>:* +@all -@dangerous`; a Mongo user gets `readWrite` on a same-named database only. `gc(job_prefix)`
removes everything named `<job_prefix>-*`, meant to run at job start (cleaning up an earlier interrupted run of
the same job name) and again at job end.

Generators reach this without changing every existing task: `generate_task()` now accepts optional `prefix`/`db`
keywords and passes them to the generator's own `generate()` only when its signature asks for them (checked with
`inspect.signature`), so the 20-odd generators that only take `(out, seed)` are untouched. A database-backed
generator declares `def generate(out, seed, prefix, db)`, calls `db.redis_user(prefix)`/`db.mongo_user(prefix)`,
and returns the credentials (and any planted secrets) in its answer key; `TrialExecutor` calls `redis_delete`/
`mongo_delete` at trial cleanup for whatever the answer key declares under `db_cleanup`, and folds
`answer_key["secrets"]` into `trial_secrets` for report redaction (previously computed for the confinement scan
only, never actually surfaced for redaction — closed as part of this task, since the evaluation-runner spec's
"Secrets in artifacts" requirement covers database passwords equally with the gateway token). Task 12.1 itself
adds no database task content (13.1/13.4): only the mechanism, exercised directly against `DatabaseAdmin` rather
than through a full task/generator, which is enough to verify its own claims.

Two bugs found only by running this live against the real containers on `envelope-linux`, neither caught by
tests against a fake transport:
1. **A freshly started container does not accept authenticated connections the instant `docker run` returns**
   (Redis needs a moment; Mongo's entrypoint creates the root user in the background before enabling auth) —
   `mongo_user()` failed with `Authentication failed` immediately after `start()`. Fixed: `start()` now polls
   `PING`/`db.adminCommand({ping:1})` after a fresh start (not when an already-running container is left alone),
   up to a timeout.
2. **`mongo_gc` enumerated live databases via `listDatabases`, which does not see a database that has a user but
   no data** — Mongo does not actually create the database until something writes to it, so a trial interrupted
   right after `createUser` (before writing any fixture rows) would be invisible to GC even though its user
   credential is still live. Fixed: enumerate by user instead, reading `db.getSiblingDB('admin').system.users`
   (readable with the admin credential regardless of which database each user's own record names).

Live results, run directly against `DatabaseAdmin` (not through the runner, matching how 10.1/10.2/11.1 were
verified — `runner/wiring.py` still only builds Windows `ssh` targets): an interrupted trial's Redis user and
Mongo user (created, never deleted) both survive until `gc(job_prefix)` removes them, and a second trial started
under the same prefix is left untouched by `gc` after being cleanly deleted first; a Redis user scoped to
`t00004:*` can write its own keys but a sibling user scoped to `t00003:*` gets `NOPERM` reading them; a Mongo
user with `readWrite` on its own database gets `not authorized` querying a sibling database. `ACL LIST` and
`system.users` show only the admin accounts afterward — no leftover per-trial objects.

### `DatabaseAdmin.start()` bug found deploying 12.2: `-p` silently drops on an internal network

Setting up the Windows lane's reverse tunnel to Mongo/Redis (task 12.2) needs them published on
`envelope-linux`'s own loopback; `docker port` showed neither container had any published port at all, despite
`start()` passing `-p 127.0.0.1:<port>:<port>` at `docker run` time. Cause: the containers were created directly
on the trial network (`--network envelope-eval`, created `--internal: true` — no route beyond the Docker host,
by design, see the `docker` target entry above), and Docker silently does not wire up host port publishing for
a container's creation network when that network is internal (confirmed empirically: the same `-p` on the
default bridge works). Fixed: `start()` now creates each container on the default bridge (`-p` works normally
there) and joins it to the trial network as a second step, `docker network connect <network> <container>` —
Docker allows a container on more than one network, and the trial network's embedded DNS still resolves the
container by name from other containers on that same network regardless of what else it's joined to, confirmed
live (`docker run --rm --network envelope-eval redis:8 redis-cli -h envelope-eval-redis ... PING` → `PONG`).

### Windows database clients installed and verified live (task 12.2, 2026-09-29)

mongosh 2.12.0 and redis-cli 5.0.14.1 (`tporadowski/redis`) installed on the VM and admitted to the enforced
WDAC policy; full detail, the scan-and-merge commands, and the fingerprint/snapshot update are in
`docs/windows-test-vm.md`. Notable beyond the `DatabaseAdmin` bug above: `Merge-CIPolicy` keeps the first
listed input's `PolicyID`/`BasePolicyID` (confirmed by parsing the merged XML before deploying, not assumed —
task 13.2's WDAC work didn't need to check this since it never risked a mismatch), and this Windows 10 21H2
build's deployed policy carries `Enabled:Update Policy No Reboot`, so `Invoke-CimMethod
PS_UpdateAndCompareCIPolicy` refreshes a redeployed `.cip` live; no `citool` (that's Windows 11 22H2+) and no
reboot were needed, unlike the original `wdac` layer's own bring-up.
