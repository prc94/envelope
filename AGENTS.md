# Repository Guidelines

## Project Structure & Module Organization

- `envelope/` contains the Python 3.12+ standard-library implementation. Protocol handling is in `envelope/protocol/`; shell discovery and launchers are in `envelope/shells/`; execution, policy, output spooling/rendering, configuration, and diagnostics each have their own package.
- `tests/unit/` covers focused behavior, `tests/integration/` runs the server with real processes and shells, and `tests/protocol/` exercises JSON-RPC/MCP transport. Shared fixtures live in `tests/fixtures/`.
- `scripts/` holds development, build, and interoperability helpers. `docs/` contains operational notes, while `openspec/` stores design/specification changes.

## Build, Test, and Development Commands

Use the Python 3.12 floor interpreter through `scripts/dev.sh`:

```sh
scripts/dev.sh lint                 # ruff check and format check
scripts/dev.sh types                # pyright (strict for envelope/)
scripts/dev.sh test -q              # full pytest suite
scripts/dev.sh test tests/unit/test_output.py -k grep
python3.12 scripts/build.py          # writes dist/envelope.pyz
```

Run `scripts/dev.sh all` before submitting a broad change. Tests may be filtered with markers such as `-m "not slow"`; available markers include `integration`, `protocol`, and `slow`.

## Coding Style & Naming Conventions

Format with Ruff (100-character lines) and keep imports/lint clean. Use four spaces, type annotations, and modern Python 3.12 syntax in normal modules. Use `snake_case` for functions, variables, and test files; `PascalCase` for classes; and lowercase package/module names. Preserve the standard-library-only runtime constraint and keep protocol diagnostics on stderr.

## Testing Guidelines

Add or update a focused pytest in the matching `tests/` area for behavior changes. Name tests `test_<behavior>` and use fixtures rather than global state. Run the smallest relevant test first, then `scripts/dev.sh test -q`; run integration/protocol tests when changing process, shell, or transport behavior.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects, consistent with history (for example, `Add envelope core: ...`). Keep commits focused. Pull requests should explain the user-visible behavior and design impact, link the relevant issue or OpenSpec change, list validation commands, and call out platform-specific or security-sensitive effects. Include logs or screenshots only when they clarify a CLI or integration change.

## Security & Configuration Notes

Envelope is not a sandbox: commands run with the caller's privileges. Do not commit secrets or machine-specific configuration. Changes affecting policy, environment scrubbing, shell launch, output limits, or process cleanup require corresponding tests and a security-focused explanation.
