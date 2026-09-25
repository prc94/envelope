# envelope — core

Stdio MCP server exposing `run` (command in a shell) and `output` (page/tail/grep a saved run output by handle `out_<8hex>`). Hard target: locked-down corp Windows (WDAC/AppLocker, CLM, GPO) → stdlib only, no native code, shipped as `dist/envelope.pyz`.

## Source map (`envelope/`)
- `cli.py` (entry `main`: serve / doctor / flags), `__main__.py`, `_pycheck.py` (py-version gate, must stay old-syntax), `app.py` (`create_services`/`create_server`/`serve`), `services.py` (`Services` container built once at startup).
- `protocol/` — own JSON-RPC 2.0 + MCP layer (`transport`, `jsonrpc`, `server` with `CallContext`/`ToolResult`). Supports 2025-11-25 (initialize handshake, default) and 2026-07-28 (stateless, `server/discover`, `_meta` protocolVersion per request).
- `tools/` — `run.py`, `output.py`, `coercion.py` (lenient arg normalization, every coercion recorded).
- `shells/` — `registry` (detection, `Platform`), `resolve` (PATH/PATHEXT lookup without spawning), `gitbash` (discovery, MSYS paths), `pathconv` (Windows↔MSYS in-process, never `cygpath`), `launch` (per-shell script delivery), `ids`.
- `execution/` — `backend.py` (`Backend`/`Process`, `default_backend`), `posix.py`, `windows.py` (ctypes Job Objects), `runner.py` (`execute`), `environment.py` (env defaults + secret scrubbing), `_shim.py` (POSIX launch shim source, run via `python -I -S -c`).
- `output/` — `spool` (framed chunk files + line index), `decode`, `cleanup` (ANSI/\r/repeat folding), `binary`, `render` (budgeted head/tail), `view` (serves `output` tool).
- `render/` — `result.py` (header `exit 1 | 2.3s | git-bash | cwd …`, text + metadata-only structuredContent), `hints.py`.
- `policy/` — `model` (`Request`/`ExecRequest`/`Decision`), `decide` (pure `make_decide`), `gateway` (single choke point; all exec must pass through it).
- `config/` (`loader`, `settings`), `doctor/report.py`, `audit.py` (JSONL), `log.py`, `paths.py`, `instructions.py`.

## Invariants
- No third-party runtime deps, no native extensions; OS integration via `ctypes` only. Avoid 3.13+ APIs (floor 3.12).
- Every run request → one policy decision → audit record, before spawning.
- Text result carries everything; `structuredContent` is metadata only (deliberate MCP deviation).
- PowerShell: never `.ps1` files, `-EncodedCommand`, `-ExecutionPolicy Bypass`, `Invoke-Expression`.
- One fresh shell process per call; stdin closed unless given.

## Planning / spec
- OpenSpec workflow in `openspec/`; active change `openspec/changes/add-envelope-core/` (design.md = decisions D1–D14, spike outcomes, roadmap P2–P4; tasks.md checklist). Later phases get their own change only when started.
- Mark tasks done: `scripts/done.sh <id> …`.

## Further memories
- Language/runtime/tooling versions and why no SDK: `mem:tech_stack`
- Dev/test/lint/build/interop commands and their env quirks: `mem:suggested_commands`
- Code style, typing strictness, module/test patterns: `mem:conventions`
- What to run before declaring a change done: `mem:task_completion`
