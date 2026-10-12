#!/bin/sh
# Start Windows screening independently while Linux screening is already running.
set -eu

cd "$(dirname "$0")/.."
run_id=${ENVELOPE_EVAL_RUN_ID:?set ENVELOPE_EVAL_RUN_ID}
job=screening-background-windows
out="eval/results/$job-$run_id"
status="eval/results/background-gates-$run_id/status.tsv"
if [ -e "$out/reproducibility.json" ]; then
    printf 'refusing to overwrite an existing run: %s\n' "$out" >&2
    exit 3
fi
mkdir -p "$out"
printf 'starting %s at %s\n' "$job" "$(date -u +%FT%TZ)"
set +e
PYTHONPATH=eval/src python3 -u -m envelope_eval run "eval/jobs/$job.toml" --out "$out" \
    > "$out/runner.log" 2>&1
result=$?
set -e
printf '%s\t%s\t%s\t%s\n' "$job" "$result" "$(date -u +%FT%TZ)" "$out" >> "$status"
printf 'finished %s with exit %s at %s\n' "$job" "$result" "$(date -u +%FT%TZ)"
exit "$result"
