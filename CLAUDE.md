# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Envelope is a stdio MCP server that runs commands in bash/sh/zsh/fish, git-bash, pwsh, Windows PowerShell 5.1, cmd and user-defined shells, exposing two tools: `run` and `output`. It ships as a single zipapp (`envelope.pyz`) run with `python envelope.pyz`.

Hard constraints (they drive most design choices):
- **Python ≥ 3.12, standard library only, no native code.** Corp targets (Windows 10 21H2 with AppLocker/WDAC, Constrained Language Mode, GPO ExecutionPolicy) only have Python 3.12.x. Don't use 3.13+ APIs or third-party runtime packages; OS integration goes through `ctypes`.
- **stdout carries only MCP protocol messages.** Diagnostics go to stderr via `envelope.log.logger`; `app.serve` redirects `sys.stdout` to stderr.
- `envelope/_pycheck.py`, `envelope/__init__.py` and the zipapp `__main__` must stay parseable by old Pythons, so the version check can report a clear error.

## Commands

`scripts/dev.sh` runs tools against the Python 3.12 floor interpreter (`uv python find 3.12`). pytest comes from the system dist-packages via `PYTHONPATH`; override with `PYTEST_PATH` or `ENVELOPE_PY`.

```sh
scripts/dev.sh lint                 # ruff check + ruff format --check
scripts/dev.sh types                # pyright (strict for envelope/, standard for tests/scripts)
scripts/dev.sh test -q              # full suite (~45 s)
scripts/dev.sh test tests/unit/test_output.py -k grep     # single file / test
scripts/dev.sh test -m "not slow"   # skip slow tests (markers: integration, protocol, slow)
python3.12 scripts/build.py         # -> dist/envelope.pyz (stdlib zipapp)
python3.12 -m envelope doctor [--json]
python3 scripts/interop.py [--harness opencode qwen] [--model ID] [--scenario basic truncation long]
```

Async tests need no plugin: `tests/conftest.py` runs `async def` tests with `asyncio.run`. Integration tests start a real `python -m envelope` over stdio (`tests/integration/client.py`, `StdioClient`) with isolated `XDG_*` dirs and assert against the JSONL audit log.

## Architecture

Request flow for `run`: `protocol/server.py` → `tools/run.py` → `tools/coercion.py` (lenient argument coercion for open models) → `shells/registry.py` (resolve shell/alias) → `policy/gateway.py` (decide + audit) → `shells/launch.py` (per-shell delivery) → `execution/runner.py` + backend → `output/spool.py` → `output/render.py` + `render/hints.py` + `render/result.py`.

- **Protocol** (`protocol/`): our own JSON-RPC layer, deliberately not the official SDK (it has native deps). Serves both `2025-11-25` (initialize handshake) and `2026-07-28` (stateless: version and client info in `_meta`, `server/discover`, `resultType`, `ttlMs`/`cacheScope`); the version is resolved per request. Each request runs in its own task. `notifications/cancelled` cancels the task and suppresses the response; stdin EOF cancels everything in flight. Progress heartbeats come from `CallContext.status` set by the tool.
- **Policy seam** (`policy/`): every execution must hold a `Permit` minted only by `Gateway.authorize`, which writes the decision to the audit log first; `runner.execute` refuses without one. `decide()` is a pure function (P1 limits: allowed shells, cwd roots, command length; audit-only mode). The future classifier (P4) plugs in here.
- **Execution** (`execution/`): thread-based pipe readers feed an asyncio queue (same code on Windows and POSIX). A call returns when the shell has exited and both pipes hit EOF; leftover detached descendants are then killed.
  - POSIX: every command runs under `execution/_shim.py`, executed as `python -I -S -c <source>` and loaded via `importlib.resources`, so it works from inside the zip. The shim is the child subreaper with PDEATHSIG, walks `/proc` to kill the whole tree, reports status on a pipe, blocks SIGTERM until its wait loop and handles it once, and resets SIGPIPE/SIGXFSZ before exec.
  - Windows (`execution/windows.py`, verified in spike S2): envelope self-job with kill-on-close, nested per-run jobs, `CREATE_SUSPENDED` + `NtResumeProcess`.
- **Script delivery** (`shells/launch.py`, verified on Windows 10 19044 in spike S1): bash family reads the command from a file in the spool dir. PowerShell and cmd are started from an outer `cmd /d /v:on … "chcp 65001>nul & <shell> …"`, because a cmd started with `CREATE_NO_WINDOW` ignores `chcp` for the rest of its own line. The command travels in env var `__ENVELOPE_PS` / `__ENVELOPE_CMD` and is expanded with delayed expansion so the outer cmd never parses it. PowerShell gets it as `-Command` (the only way under Constrained Language Mode, which forbids `[scriptblock]::Create`), wrapped in a prologue/epilogue that maps `$LASTEXITCODE`/`$?` and writes a non-terminating-error count to a status file. Past cmd's 8,191-char line: CLM → tool error; FullLanguage → direct `-Command` up to 32 K, then a `.txt` data file + `[scriptblock]::Create`. Never use `-EncodedCommand`, `-ExecutionPolicy Bypass`, `Invoke-Expression` or `.ps1` files.
- **Windows testing**: VM `admin@`/`envelope@192.168.94.172` (SSH agent key; test as `envelope`, a standard user). Over SSH a standard user gets "Access denied" from WMI, `tasklist` and `taskkill`, so inspect processes with Toolhelp32 via ctypes. Toggle CLM with `setx /M __PSLockdownPolicy 4` as admin and remove it afterwards.
- **Output** (`output/`): the spool is per handle (`out_<8 hex>`) under the cache dir. It holds raw framed chunks, decoded line records and an on-disk u64 line-offset index, with head/tail segments under a per-run cap and dropped middles tracked. Line numbers are global across the interleaved streams. Decoding is decided per stream early (BOM → UTF-16 pattern → binary → UTF-8 with console-code-page fallback for cmd/powershell). Cleanup (ANSI, `\r`/backspace, folding ≥3 repeats) happens only at render time; the spool keeps raw bytes. `render.py` does budgeted head/tail (1/3 head, 2/3 tail) and never reads across a dropped gap.
- **Results**: the text carries everything (header `exit N | 1.23s | shell | cwd …`, truncation line, body, `note:` hints); `structuredContent` is metadata only. Hints are only emitted for certain conditions (catalog in `render/hints.py`).
- **Config** (`config/`): defaults < TOML file < `ENVELOPE_*` env < `--flags`. Every key is declared in `Settings` plus a parser in `PARSERS` (the assert keeps them in sync). Invalid values are fatal; unknown keys warn.

## Planning (OpenSpec)

Work is spec-driven with OpenSpec (`openspec/`; `/opsx:*` commands and `openspec-*` skills in `.claude/`). The active change is `openspec/changes/add-envelope-core/` (P0 spikes + P1 core): `tasks.md` tracks status; `design.md` holds the decisions (D1–D14), spike outcomes and the roadmap (P2 evaluation harness → P3a/P3b background & interactive → P4 security classifier). Each later phase is proposed as its own change just before it is built. Mark a task `[x]` only when its stated verification has actually been done (several remain open pending a Windows/macOS host or CI). Windows test VM plan: `docs/windows-test-vm.md`.

## Interop runs

`scripts/interop.py` drives OpenCode and Qwen Code via OpenRouter against `dist/envelope.pyz`. Score from envelope's audit log, not from the model's answer. Harness pitfalls handled there:
- OpenCode needs `--dir` and a pinned `small_model`; otherwise its title agent calls Gemini, which the OpenRouter key's guardrail blocks.
- Qwen Code silently substitutes models for `qwen/...` ids in `modelProviders`, so select via `OPENAI_*` env.
- Qwen Code project MCP servers need `qwen mcp approve envelope`.
