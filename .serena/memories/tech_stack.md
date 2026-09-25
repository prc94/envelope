# Tech stack

- Python ≥3.12, standard library only (`asyncio`, `ctypes`, `tomllib`, `zipapp`). `pyproject.toml` `dependencies = []`; package name `envelope-mcp`, version from `envelope.__version__`.
- Official MCP Python SDK deliberately NOT used (pydantic-core/rpds-py/pywin32 native deps blocked by WDAC) — protocol layer is hand-written in `envelope/protocol/`.
- Distribution: `scripts/build.py` → `dist/envelope.pyz` (zipapp; generated `__main__` calls `_pycheck.ensure_supported()` before importing anything modern).
- Dev tools (not project deps, installed user-wide in `~/.local/bin`): `ruff` (line-length 100, py312, rules E,F,W,I,UP,B,SIM,RUF), `pyright` (standard mode; strict for `envelope/`), `pytest` (from system dist-packages), `uv` (only used to locate a 3.12 interpreter).
- Test floor interpreter: uv-managed CPython 3.12; system `/usr/bin/python3` is 3.14.
- Target platforms: Linux, Windows (primary corp target, git-bash/pwsh/powershell 5.1/cmd), macOS (best-effort containment).
- Interop clients: OpenCode, Qwen Code (via OpenRouter models) — see `scripts/interop.py`.
