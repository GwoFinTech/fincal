#!/usr/bin/env bash
# The **only** scheduling entrypoint of the FinCal sync pipeline (README
# "Sync scheduling contract").  A scheduler must call this file and must not
# keep its own copy of the stage list: a wrapper outside the repo that called
# three of the five stages is how `consensus` and `stock_names` went 47 days
# without a run while every health endpoint stayed green (Issue #57).
#
#   1. run the pipeline            → scripts/sync_all.sh  (stage list lives there)
#   2. run the freshness gate      → scripts/check_sync_freshness.py
#
# The gate runs *after* the stages on purpose: judging freshness first would
# fail the job before a catch-up run has had a chance to refresh the data.
# Any failure (a stage that failed, or a stage/derived table that is stale)
# makes this entrypoint exit non-zero so the scheduled job fails loudly
# instead of quietly serving 6-week-old data.
#
# Exit codes: 0 = pipeline ran and everything is fresh, 1 = stage failure or
# stale data, 2 = freshness could not be determined (database unreadable).
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${APP_DIR}"
export DB_HOST=localhost

if [[ "${_FINCAL_CRON_RUNNING:-}" == "1" ]]; then
    echo "ERROR: cron_sync.sh called recursively" >&2
    exit 1
fi
export _FINCAL_CRON_RUNNING=1

GATE_TIMEOUT="${FINCAL_FRESHNESS_TIMEOUT:-300}"

pipeline_status=0
"${SCRIPT_DIR}/sync_all.sh" || pipeline_status=$?

echo "=== freshness gate [$(date '+%F %T')] ==="
gate_status=0
timeout --kill-after=30 "${GATE_TIMEOUT}" uv run python scripts/check_sync_freshness.py || gate_status=$?

if (( gate_status == 124 )); then
    echo "FRESHNESS GATE FAILED: timed out after ${GATE_TIMEOUT}s" >&2
    gate_status=1
elif (( gate_status != 0 )); then
    echo "FRESHNESS GATE FAILED (exit ${gate_status}): the stale/never-run stages or derived data are named above (Issue #57)" >&2
fi

overall=0
(( pipeline_status == 0 )) || overall=1
(( gate_status == 0 )) || overall=1

echo "=== FinCal scheduled run finished (pipeline_status=${pipeline_status} gate_status=${gate_status} status=${overall}) ==="
exit "${overall}"
