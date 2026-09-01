#!/usr/bin/env bash
# Cron wrapper: run the full sync pipeline from this installation.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${_FINCAL_CRON_RUNNING:-}" == "1" ]]; then
    echo "ERROR: cron_sync.sh called recursively" >&2
    exit 1
fi
export _FINCAL_CRON_RUNNING=1
exec "${SCRIPT_DIR}/sync_all.sh"
