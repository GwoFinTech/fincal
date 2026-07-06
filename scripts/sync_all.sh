#!/usr/bin/env bash
# Full sync: Longbridge calendar → Futu actuals/dates → predict future quarters
# Designed to be called by cron. Uses the fincal venv.
set -euo pipefail
cd /opt/fincal
export DB_HOST=localhost

# Step 1: Sync calendar from Longbridge (estimates + actuals + dates)
uv run python scripts/sync_earnings.py 2>&1

# Step 2: Sync actuals + dates from Futu (more reliable actuals)
uv run python scripts/sync_futu.py 2>&1

# Step 3: Confirm predicted rows that now have real data, then predict future
uv run python scripts/predict_earnings.py 2>&1

echo "=== FinCal weekly sync complete ==="
