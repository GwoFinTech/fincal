#!/usr/bin/env bash
# Deploy fincal: sync source → /opt, rebuild, smoke test (Issues #14, #20).
set -euo pipefail
SRC="/root/src-aigen/fincal/"
DST="/opt/fincal/"

COMMIT=$(git -C "$SRC" rev-parse --short HEAD)
echo "=== Deploying fincal $COMMIT ==="

# Pre-deploy: check working tree is clean
if [ -n "$(git -C "$SRC" status --porcelain)" ]; then
    echo "WARNING: working tree has uncommitted changes"
fi

# Frontend asset gate (Issue #42): the SPA must not depend on runtime CDNs.
# The vendored Vue prod build and the prerendered Tailwind CSS are committed
# artifacts; fail the deploy if they are missing/empty or if index.html still
# references a runtime CDN (unpkg / cdn.tailwindcss.com). fonts.googleapis.com
# is a progressive enhancement only (system-ui fallback), so it is allowed.
VENDOR="$SRC/app/static/assets/vendor/vue.global.prod.js"
FRONTEND_CSS="$SRC/app/static/assets/tailwind.css"
INDEX_HTML="$SRC/app/static/index.html"
echo "=== Frontend asset gate (Issue #42) ==="
if [ ! -s "$VENDOR" ]; then
    echo "FAIL: vendor Vue prod build missing or empty: $VENDOR"; exit 1
fi
if [ ! -s "$FRONTEND_CSS" ]; then
    echo "FAIL: prerendered tailwind.css missing or empty: $FRONTEND_CSS"; exit 1
fi
if grep -qE 'unpkg\.com|cdn\.tailwindcss\.com' "$INDEX_HTML"; then
    echo "FAIL: index.html still references a runtime CDN (unpkg/cdn.tailwindcss.com)"; exit 1
fi
echo "Frontend assets OK"

echo "=== Syncing: $SRC → $DST ==="
rsync -av --delete \
  --exclude='.env' \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$SRC" "$DST"

# Write version file
echo "$COMMIT" > "$DST/VERSION"

# Scheduling-contract gate (Issue #57).  The weekly job must run the repo
# entrypoint, and that entrypoint must reach every declared stage plus the
# freshness gate: the caller used to keep its own copy of the stage list
# (three of five stages), which is how `consensus` / `stock_names` went 47
# days without a run while every health endpoint stayed green.
echo "=== Sync scheduling contract (Issue #57) ==="
for name in cron_sync.sh sync_all.sh; do
    repo_sum=$(sha256sum "$SRC/scripts/$name" | awk '{print $1}')
    dst_sum=$(sha256sum "$DST/scripts/$name" | awk '{print $1}')
    if [ "$repo_sum" != "$dst_sum" ]; then
        echo "FAIL: deployed script differs from the repo: $DST/scripts/$name"; exit 1
    fi
done

ENTRYPOINT="$DST/scripts/cron_sync.sh"
STAGE_LIST=$(grep -oE 'python scripts/[a-z_]+\.py' "$DST/scripts/sync_all.sh" \
    | awk '{print $2}' | tr '\n' ' ')
echo "entrypoint : $ENTRYPOINT"
echo "stages     : $STAGE_LIST"
if ! grep -q 'scripts/sync_all.sh' "$ENTRYPOINT"; then
    echo "FAIL: $ENTRYPOINT does not call the pipeline (scripts/sync_all.sh)"; exit 1
fi
if ! grep -q 'scripts/check_sync_freshness.py' "$ENTRYPOINT"; then
    echo "FAIL: $ENTRYPOINT does not call the freshness gate (scripts/check_sync_freshness.py)"; exit 1
fi

# Cross-check the deployed stage list against the registry the freshness gate
# and /api/admin/health use, so a stage that only exists in one of the two
# cannot ship (tests do the same from source; this checks the deployed copy).
REGISTRY_PY="$SRC/.venv/bin/python"
if [ -x "$REGISTRY_PY" ]; then
    REGISTRY=$("$REGISTRY_PY" -c 'import sys; sys.path.insert(0, sys.argv[1]);\
from app.freshness import STAGE_SCRIPTS; print(" ".join(sorted(STAGE_SCRIPTS.values())))' "$SRC")
    DEPLOYED_SORTED=$(printf '%s\n' $STAGE_LIST | sort | tr '\n' ' ' | sed 's/ $//')
    if [ "$REGISTRY" != "$DEPLOYED_SORTED" ]; then
        echo "FAIL: stage drift between scripts/sync_all.sh and app.freshness.STAGE_SCRIPTS"
        echo "  sync_all.sh : $DEPLOYED_SORTED"
        echo "  registry    : $REGISTRY"
        exit 1
    fi
    echo "registry   : matches ($REGISTRY)"
else
    echo "WARNING: $REGISTRY_PY not found — skipped the registry cross-check"
fi

# Scheduler-side wrappers are outside the repo, so only a warning can be
# raised here: repointing the scheduler at the entrypoint is a host action.
# Override the search list with FINCAL_SCHEDULER_WRAPPERS.
SCHEDULER_WRAPPERS="${FINCAL_SCHEDULER_WRAPPERS:-/root/.hermes/scripts/*fincal*.sh}"
scheduler_drift=0
for wrapper in $SCHEDULER_WRAPPERS; do
    [ -f "$wrapper" ] || continue
    if grep -q 'scripts/cron_sync.sh' "$wrapper"; then
        echo "scheduler  : $wrapper delegates to scripts/cron_sync.sh (ok)"
    else
        scheduler_drift=1
        echo "scheduler  : $wrapper does NOT delegate to scripts/cron_sync.sh"
        echo "             its own stage list:"
        grep -nE 'sync_[a-z_]+\.py|predict_earnings\.py' "$wrapper" | sed 's/^/               /' || true
    fi
done
if [ "$scheduler_drift" = "1" ]; then
    echo "ACTION REQUIRED (Issue #57): point the weekly job at the repo entrypoint"
    echo "  replace the wrapper body with: exec $ENTRYPOINT"
    echo "  then verify the next run produced all five stages:"
    echo "  SELECT stage,status,count(*) FROM sync_runs GROUP BY 1,2;"
fi

echo "=== Building ==="
cd "$DST"
docker compose build --no-cache 2>&1 | tail -3

echo "=== Starting ==="
docker compose up -d 2>&1

echo "=== Smoke test (waiting 5s) ==="
sleep 5

# Check container is running
if ! docker ps --filter name=fincal --format '{{.Names}}' | grep -q fincal; then
    echo "FAIL: container not running"
    docker logs fincal --tail 20
    exit 1
fi

# Check readiness
READY=$(docker exec fincal python -c "
from app.db import db_cursor
try:
    with db_cursor() as cur:
        cur.execute('SELECT 1')
    print('ready')
except:
    print('not_ready')
" 2>/dev/null)

if [ "$READY" != "ready" ]; then
    echo "FAIL: readiness check returned '$READY'"
    exit 1
fi

echo "=== Done — fincal $COMMIT deployed and ready ==="
