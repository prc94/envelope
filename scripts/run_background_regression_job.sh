#!/bin/sh
# Run one corrected regression job; separate units allow Linux and Windows to overlap.
set -eu

cd "$(dirname "$0")/.."
job=${1:?give regression job name}
run_id=${2:?give run ID}
case "$job" in
    regression-background-linux|regression-background-windows) ;;
    *) printf 'unsupported job: %s\n' "$job" >&2; exit 2 ;;
esac
out="eval/results/$job-$run_id"
if [ -e "$out" ]; then
    printf 'refusing to overwrite %s\n' "$out" >&2
    exit 3
fi
mkdir -p "$out"
printf 'starting %s at %s\n' "$job" "$(date -u +%FT%TZ)"
set +e
PYTHONPATH=eval/src python3 -u -m envelope_eval run "eval/jobs/$job.toml" --out "$out" \
    > "$out/runner.log" 2>&1
result=$?
set -e
printf 'finished %s with exit %s at %s\n' "$job" "$result" "$(date -u +%FT%TZ)"
exit "$result"
