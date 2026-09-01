#!/usr/bin/env bash
# Full sync: Longbridge calendar → Futu actuals/dates → consensus → predict future quarters
# Designed to be called by cron. Uses the fincal venv.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${APP_DIR}"
export DB_HOST=localhost

# Step 1: Sync calendar from Longbridge (estimates + actuals + dates)
uv run python scripts/sync_earnings.py 2>&1

# Step 2: Sync actuals + dates from Futu (more reliable actuals)
uv run python scripts/sync_futu.py 2>&1

# Step 3: Resolve names for symbols that were not named by the calendar source.
uv run python scripts/sync_stock_names.py 2>&1

# Step 4: Sync Longbridge quarterly consensus and forecast-EPS revision range.
uv run python scripts/sync_consensus.py 2>&1

# Step 5: Confirm predicted rows that now have real data, then predict future
uv run python scripts/predict_earnings.py 2>&1

echo "=== FinCal weekly sync complete ==="
