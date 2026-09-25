# Conventions

- Regular modules: one-line module docstring, then `from __future__ import annotations` (not needed in `__main__.py` or tiny `__init__.py` re-export modules; forbidden in the two old-syntax files below). Absolute imports (`from envelope.x import y`), ruff isort ordering.
- Full type hints; pyright strict on `envelope/`. Use `collections.abc` types, `X | None`, dataclasses (often frozen) for value objects.
- Exceptions to modern style: `envelope/_pycheck.py` (must parse on any Python 3; `# pyright: basic`, no annotations) and `envelope/execution/_shim.py` (run standalone with `-I -S`, only stdlib `os`/`signal`/`sys`, `%`-formatting, `# pyright: basic`). Both have ruff per-file ignores — keep them that way.
- Windows-only code is type-checked too (`reportUnnecessaryComparison = "none"` to allow `sys.platform` branches).
- Dependency wiring: long-lived objects live in `Services` (`services.py`), built in `app.create_services`; tools receive `Services`, not globals. Backends are injectable (`Backend`) for tests.
- Policy is a pure function `(Request, Settings) -> Decision` created by `make_decide(windows=…)`; enforcement only in `policy/gateway.py`.
- Tool argument handling: declare `Param(...)` tuples and go through `tools/coercion.coerce`; user mistakes become tool errors (actionable text), not protocol errors.
- User-facing text (results, hints, errors, config errors) is terse, deterministic and tells the model what to do next (e.g. how to fetch omitted output via `output(...)`).
- Comments sparse; explain *why* (platform quirks), not what.
- Tests: `tests/unit`, `tests/integration` (real shells via `StdioClient`, `pytestmark = [pytest.mark.integration, posix_only]`), `tests/protocol` (scripted JSON-RPC harness). Async tests are plain `async def` — a root `conftest.py` hook runs them with `asyncio.run`; no pytest-asyncio or other plugins. Dev deps = pytest only.
- Planning docs and spec deltas go through OpenSpec (`openspec/changes/...`), not ad-hoc docs.
