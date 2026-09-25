# envelope

A compact MCP server that lets coding agents run commands in **bash, sh, zsh, fish,
git-bash, PowerShell 7, Windows PowerShell 5.1, cmd** and user-defined shells, and returns
bounded, model-friendly results. It is a single file (`envelope.pyz`) that runs on a stock
CPython 3.12+ interpreter with no installation, no third-party packages and no native code,
so it also works on locked-down Windows machines (AppLocker/WDAC, Constrained Language Mode,
GPO execution policy) where downloaded executables are blocked.

Two tools:

- **`run`**: runs a command or multi-line script in a fresh shell process, with `cwd`,
  `timeout_s`, `stdin` and `env`. It returns a one-line header (`exit 1 | 2.3s | git-bash | cwd C:\src\app`),
  the cleaned output (ANSI codes and progress-bar redraws removed, repeated lines folded, head and tail
  kept within a budget) and deterministic hints (command not found in this shell but available in
  another one, killed by SIGKILL, blocked by application control, …).
- **`output`**: pages, tails or greps the complete saved output of a previous `run`, using the handle
  (`out_1a2b3c4d`) given in its result.

## Installation

Download `envelope.pyz` (or build it: `python3 scripts/build.py` → `dist/envelope.pyz`) and run
it with Python 3.12 or newer (`python3` on Linux/macOS; `python` or `py` on Windows, where a bare
`python3` often does not exist):

```sh
python3 envelope.pyz --version
python3 envelope.pyz doctor          # how envelope sees this machine
python3 envelope.pyz doctor --json   # same, machine-readable
```

Without arguments (or with `serve`) envelope serves MCP over stdio. Register it in your client
with the full path of the interpreter and of `envelope.pyz`.

### OpenCode

`opencode.json` in the project (or `~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "envelope": {
      "type": "local",
      "command": ["python3", "/path/to/envelope.pyz"],
      "enabled": true
    }
  }
}
```

On Windows use e.g. `["C:\\Python312\\python.exe", "C:\\tools\\envelope.pyz"]`. Check the
connection with `opencode mcp list`.

### Qwen Code

`.qwen/settings.json` in the project (or `~/.qwen/settings.json`):

```json
{
  "mcpServers": {
    "envelope": {
      "command": "python3",
      "args": ["/path/to/envelope.pyz"]
    }
  }
}
```

Project-level servers must be approved once: `qwen mcp approve envelope`. Check with
`qwen mcp list`.

Any MCP client that speaks protocol `2025-11-25` (initialize handshake) or `2026-07-28`
(stateless, `server/discover`) over stdio works the same way.

## How commands run

- **One process per call.** `cd`, `export` and shell variables do not carry over between calls; pass
  `cwd` and `env` instead.
- **Stdin is closed** unless `stdin` is given, so interactive prompts fail fast instead of hanging.
- **Non-interactive environment:** `NO_COLOR=1`, `TERM=dumb`, `PAGER=cat`, `GIT_PAGER=cat`,
  `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `DEBIAN_FRONTEND=noninteractive`,
  `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8` (configurable; `env` wins).
- **Secrets are not passed on:** inherited variables matching `*_API_KEY`, `*_TOKEN`, `*_SECRET` and
  `OPENROUTER_*` are removed (configurable; variables passed explicitly in `env` are kept).
- **Script delivery:** bash-family shells read the command from a file in the spool directory, so
  there is no command-line limit. PowerShell gets the script as data (a `.txt` file read with
  `Get-Content -Raw` and run as a script block). envelope never creates `.ps1` files and never uses
  `-EncodedCommand`, `-ExecutionPolicy Bypass` or `Invoke-Expression`. cmd receives the command
  line directly and is limited to 8,191 characters.
- **Timeouts and cleanup:** default 120 s, maximum 3,600 s. When a command times out or is
  cancelled, its whole process tree is killed. If envelope itself dies, the commands it started
  are killed too. On Linux the launch shim is a child subreaper with a parent-death signal. On
  Windows, envelope and each run are placed in kill-on-close Job Objects. On macOS, cleanup is
  best-effort: descendants that call `setsid()` can escape.
- **Output:** stdout and stderr are captured separately, in arrival order. They are decoded as
  UTF-8, as UTF-16, or in the console code page for cmd and Windows PowerShell, and saved in full
  to the spool (up to 64 MiB per run, keeping head and tail). Results show at most 12,000
  characters and 400 lines. When output is cut, the result says exactly how to get the rest with
  `output(...)`.

## Configuration

Settings come from, in increasing precedence: built-in defaults; a TOML file
(`$XDG_CONFIG_HOME/envelope/config.toml`, `~/Library/Application Support/envelope/config.toml`
or `%APPDATA%\envelope\config.toml`, or the path given by `--config` / `ENVELOPE_CONFIG`);
`ENVELOPE_<KEY>` environment variables; `--<key>` command-line flags (for example
`--default-timeout-s 30`, or the short form `--default-timeout 30`).

| key | default | meaning |
|---|---|---|
| `default_shell` | automatic | Windows: git-bash → pwsh → powershell → cmd; macOS/Linux: login shell if supported → bash → sh |
| `shells` | `{}` | custom shells, see below |
| `default_cwd` | start directory | working directory when `cwd` is omitted |
| `default_timeout_s` / `max_timeout_s` | 120 / 3600 | timeouts; larger requests are clamped with a note |
| `max_concurrency` | 8 | concurrent `run` calls; waiting counts toward the timeout |
| `progress_interval_s` | 5 | progress notification interval (when the client sends a progress token) |
| `env_defaults` | see above | environment defaults for every command |
| `env_scrub` | `["*_API_KEY", "*_TOKEN", "*_SECRET", "OPENROUTER_*"]` | inherited variables to remove |
| `output_max_chars` / `output_max_lines` | 12000 / 400 | output budget per result |
| `spool_dir` | OS cache dir | where full outputs are kept |
| `spool_cap_mib` / `spool_total_mib` / `spool_retention_h` | 64 / 1024 / 24 | per-run cap, total size, retention |
| `allowed_shells` | all | policy: shells that may be used |
| `allowed_cwd_roots` | unrestricted | policy: working directories must lie below one of these |
| `max_command_chars` | 1000000 | policy: maximum command length |
| `audit_only` | false | log policy decisions without enforcing them |
| `audit_enabled` / `audit_log` | true / OS state dir | JSONL audit log of every call and decision |
| `log_level` | `warn` | stderr diagnostics: `error`, `warn`, `info`, `debug` |

Policy keys can also be put in a `[policy]` table. Unknown keys produce a warning. Invalid values
stop envelope with an error that names the key, the value and the expected form.

Custom shell example:

```toml
default_shell = "pwsh"

[shells.nu]
path = "nu"                      # name on PATH or full path
args = ["-c", "{script}"]        # {script} = command text, {file} = path of a script file
extension = ".nu"                # extension of the {file} script (optional)

[policy]
allowed_cwd_roots = ["C:\\work"]
```

## Security posture

**envelope is not a sandbox.** Commands run with the full rights of the user who started
envelope. The execution policy (allowed shells, working-directory roots, command length, and
audit-only mode) is a guardrail against mistakes, not an isolation boundary. Every request passes
through one policy decision and is written to the audit log before anything runs. Environment
scrubbing keeps harness API keys away from commands, but it cannot stop a command from reading
files the user can read. If you need isolation, run envelope inside a container, VM or dedicated
account.

## Development

```sh
scripts/dev.sh lint     # ruff check + format check
scripts/dev.sh types    # pyright (strict for envelope/)
scripts/dev.sh test     # pytest on the Python 3.12 floor interpreter
python3 scripts/build.py # dist/envelope.pyz
```
