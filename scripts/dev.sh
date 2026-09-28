#!/bin/sh
# Dev helper: run a tool against the Python 3.12 floor interpreter.
# Usage: scripts/dev.sh test [pytest args] | lint | types | all
set -e
cd "$(dirname "$0")/.."
PY="${ENVELOPE_PY:-$(uv python find 3.12)}"
case "$1" in
  test) shift; PYTHONPATH="${PYTEST_PATH:-/usr/lib/python3/dist-packages}:$PWD/eval/src:$PWD/eval" "$PY" -m pytest tests eval/tests "$@" ;;
  lint) ruff check . && ruff format --check . ;;
  types) pyright --pythonpath "${PYRIGHT_PY:-/usr/bin/python3}" && (cd eval && pyright --pythonpath "${PYRIGHT_PY:-/usr/bin/python3}") ;;
  all) "$0" lint; "$0" types; "$0" test -q ;;
  *) echo "usage: $0 test|lint|types|all" >&2; exit 2 ;;
esac
