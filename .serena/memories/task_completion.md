# Task completion checklist

1. `ruff format .` (then) `scripts/dev.sh all` — lint + format check, pyright (strict on `envelope/`), full pytest on Python 3.12. All must be clean.
2. If runtime code changed and interop/manual smoke matters: `python3 scripts/build.py` and `python3 dist/envelope.pyz doctor`.
3. If the work corresponds to an OpenSpec task in `openspec/changes/add-envelope-core/tasks.md`: tick it with `scripts/done.sh <id>`; record spike outcomes/decisions in that change's `design.md` if applicable.
4. README configuration table / behavior sections must stay in sync with `config/settings.py` defaults and tool behavior.
