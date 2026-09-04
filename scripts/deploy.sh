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
