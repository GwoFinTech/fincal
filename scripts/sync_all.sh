#!/usr/bin/env bash
# Full sync: Longbridge calendar → Futu actuals/dates → stock names → consensus → predict.
#
# This file *is* the pipeline contract: the `run_stage` lines below are the
# stage list that `app/freshness.py::STAGE_SCRIPTS` registers and that
# `tests/test_sync_freshness.py` parses.  Adding a stage means adding one
# `run_stage` line here **and** one entry in `STAGE_SCRIPTS`.
#
# Execution policy (Issue #57 — moved in from the out-of-repo cron wrapper):
#
#   * every stage runs under `timeout` so a hung provider cannot stall the
#     weekly job (per-stage budget, overridable via FINCAL_STAGE_TIMEOUT_*);
#   * a failing stage never prevents the remaining stages from running —
#     the old `set -e` aborted the whole pipeline on the first failure, which
#     is how `stock_names` / `consensus` silently stopped being reached;
#   * each stage logs to its own file, only a filtered tail reaches stdout,
#     and a failed stage keeps its log so provider errors are not discarded;
#   * a per-stage summary (status + seconds) closes the run, so a stage that
#     never ran cannot hide in a long log.
#
# Exit code: 0 only when every stage succeeded; 1 otherwise (never silently).
# The freshness gate lives one level up, in `scripts/cron_sync.sh`.
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${APP_DIR}"
export DB_HOST=localhost

# Per-stage wall-clock budget in seconds.  `consensus` is the slowest stage
# (133 symbols × 3 Longbridge CLI calls with pacing), so it gets the widest
# budget; a timeout only leaves partial upserts, which are idempotent.
TIMEOUT_LONGBRIDGE="${FINCAL_STAGE_TIMEOUT_LONGBRIDGE:-900}"
TIMEOUT_FUTU="${FINCAL_STAGE_TIMEOUT_FUTU:-1500}"
TIMEOUT_STOCK_NAMES="${FINCAL_STAGE_TIMEOUT_STOCK_NAMES:-900}"
TIMEOUT_CONSENSUS="${FINCAL_STAGE_TIMEOUT_CONSENSUS:-2400}"
TIMEOUT_PREDICTION="${FINCAL_STAGE_TIMEOUT_PREDICTION:-600}"

LOG_DIR="$(mktemp -d "/tmp/fincal-sync.XXXXXX")"
STAGE_STATUS=()
KEEP_LOGS=0

cleanup() {
    if (( KEEP_LOGS == 0 )); then
        rm -rf "${LOG_DIR}"
    else
        echo "stage logs kept in ${LOG_DIR}"
    fi
}
trap cleanup EXIT

# run_stage <name> <timeout_seconds> <command...>
run_stage() {
    local name="$1" budget="$2"
    shift 2
    local log="${LOG_DIR}/${name}.log"
    local start end status

    echo "=== ${name} (timeout ${budget}s) [$(date '+%F %T')] ==="
    start=$(date +%s)
    # No --foreground: timeout then runs the stage in its own process group and
    # signals the whole group, so a hung `uv run python` child (provider call
    # with no timeout, Issue #49) cannot survive the budget.  --kill-after
    # escalates to SIGKILL when SIGTERM is ignored.
    if timeout --kill-after=30 "${budget}" "$@" >"${log}" 2>&1; then
        status=0
    else
        status=$?
    fi
    end=$(date +%s)

    # Keep the run readable for cron: operational lines only, then a verdict.
    grep -E '(INFO|WARNING|ERROR|Sync complete|Flushed|Futu earnings|Futu actuals|Prediction complete|Connected|unavailable)' \
        "${log}" | tail -30 || true

    if (( status == 124 || status == 137 )); then
        # 124 = the budget elapsed, 137 = SIGKILL after --kill-after.
        echo "${name}: TIMED OUT after ${budget}s (log: ${log})"
        STAGE_STATUS+=("${name}|timeout|$((end - start))")
    elif (( status != 0 )); then
        echo "${name}: FAILED with exit ${status} (log: ${log})"
        tail -40 "${log}" || true
        STAGE_STATUS+=("${name}|failed|$((end - start))")
    else
        echo "${name}: completed in $((end - start))s"
        STAGE_STATUS+=("${name}|ok|$((end - start))")
        rm -f "${log}"
    fi
    return "${status}"
}

overall=0
run_stage longbridge  "${TIMEOUT_LONGBRIDGE}"  uv run python scripts/sync_earnings.py   || { overall=1; KEEP_LOGS=1; }
run_stage futu        "${TIMEOUT_FUTU}"        uv run python scripts/sync_futu.py       || { overall=1; KEEP_LOGS=1; }
run_stage stock_names "${TIMEOUT_STOCK_NAMES}" uv run python scripts/sync_stock_names.py || { overall=1; KEEP_LOGS=1; }
run_stage consensus   "${TIMEOUT_CONSENSUS}"   uv run python scripts/sync_consensus.py  || { overall=1; KEEP_LOGS=1; }
run_stage prediction  "${TIMEOUT_PREDICTION}"  uv run python scripts/predict_earnings.py || { overall=1; KEEP_LOGS=1; }

echo "--- stage summary ---"
for entry in "${STAGE_STATUS[@]}"; do
    IFS='|' read -r name status seconds <<<"${entry}"
    printf '%-12s %-8s %ss\n' "${name}" "${status}" "${seconds}"
done
echo "=== FinCal weekly sync finished (status=${overall}) ==="
exit "${overall}"
