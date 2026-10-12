#!/bin/sh
# Run the four P3a eval jobs on a machine that can reach both targets and the gateway.
set -u

cd "$(dirname "$0")/.." || exit 1
run_id=${ENVELOPE_EVAL_RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}
root="eval/results/background-gates-$run_id"
mkdir -p "$root"
status="$root/status.tsv"
printf 'job\texit\tfinished_utc\toutput\n' > "$status"

for job in regression-background-linux regression-background-windows \
    screening-background-linux screening-background-windows; do
    out="eval/results/$job-$run_id"
    if [ -e "$out/reproducibility.json" ]; then
        if [ -e "$out/report/summary.json" ]; then
            printf 'already finished %s: %s\n' "$job" "$out"
            continue
        fi
        printf 'refusing to overwrite an unfinished run: %s\n' "$out" >&2
        exit 3
    fi
    mkdir -p "$out"
    printf 'starting %s at %s\n' "$job" "$(date -u +%FT%TZ)"
    PYTHONPATH=eval/src python3 -u -m envelope_eval run "eval/jobs/$job.toml" --out "$out" \
        > "$out/runner.log" 2>&1
    result=$?
    printf '%s\t%s\t%s\t%s\n' "$job" "$result" "$(date -u +%FT%TZ)" "$out" >> "$status"
    printf 'finished %s with exit %s at %s\n' "$job" "$result" "$(date -u +%FT%TZ)"
done
