# Suggested commands

Run from repo root.

- `scripts/dev.sh lint` — `ruff check .` + `ruff format --check .`
- `scripts/dev.sh types` — pyright; uses `--pythonpath ${PYRIGHT_PY:-/usr/bin/python3}` (so pytest stubs resolve).
- `scripts/dev.sh test [pytest args]` — pytest on the 3.12 floor interpreter (`${ENVELOPE_PY:-$(uv python find 3.12)}`) with `PYTHONPATH=${PYTEST_PATH:-/usr/lib/python3/dist-packages}` to borrow system pytest. Do not just run `pytest` with system python (3.14) — it won't catch 3.13+ API use.
- `scripts/dev.sh all` — lint, types, test -q.
- Subsets: `scripts/dev.sh test tests/unit`, `-m "not integration"`, `-m protocol`. Markers: `integration` (real shells), `protocol` (stdio MCP), `slow`. Integration tests are POSIX-only on this box.
- Build: `python3 scripts/build.py` → `dist/envelope.pyz`.
- Smoke: `python3 dist/envelope.pyz --version`, `… doctor [--json]`.
- Interop (costs OpenRouter credits, needs OpenCode/Qwen Code installed): `python3 scripts/interop.py [--harness opencode qwen] [--model ID …] [--scenario NAME …]`, `--setup-only`. Uses `/tmp/envelope-interop` (override `ENVELOPE_INTEROP_DIR`); rebuild the pyz first.
- OpenSpec task tick: `scripts/done.sh 6.3 7.1`.
