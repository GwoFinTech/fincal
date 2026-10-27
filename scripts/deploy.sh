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

# Test gate (Issue #98): the suite is the only regression defence for the
# #37–#97 fixes, and it used to depend on the production database being
# reachable — so a change that broke, say, the iCal body or the comparison
# attribution could still print "deployed and ready". The default collection is
# fully offline (tests/conftest.py refuses real connections), so this runs
# before the rsync and a red suite never reaches /opt. Real-database checks are
# opt-in (`--run-integration`) and are not part of the deploy path.
TEST_PY="$SRC/.venv/bin/python"
echo "=== Test gate (Issue #98) ==="
if [ ! -x "$TEST_PY" ]; then
    echo "FAIL: $TEST_PY not found — cannot run the test gate"; exit 1
fi
if ! ( cd "$SRC" && "$TEST_PY" -m pytest -q ); then
    echo "FAIL: pytest failed — not deploying"; exit 1
fi
echo "tests OK"

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

ENTRYPOINT="${DST%/}/scripts/cron_sync.sh"
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
# The commit is injected into the image (Issue #79) so the container can attest
# what it is: `Dockerfile` copies only `app/`, so the VERSION file written below
# never reaches it and `app/version.py` used to answer "dev" on every deploy.
export GIT_COMMIT="$COMMIT"
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

# Dependency-address gate (Issue #70).  The Kurumi probe used to fetch FinCal's
# own `/api/config` because KURUMI_API_URL defaults to http://localhost:8000 —
# inside the container that is this service, so the "external dependency" check
# could never fail, and the variable was documented nowhere.  Ask the running
# container for the verdict and its address (warn only: repointing the address
# is a host/network decision).
echo "=== Kurumi dependency address (Issue #70) ==="
KURUMI_VERDICT=$(docker exec fincal python -c "
import json
from app.company_name import probe_kurumi, kurumi_target_label
from app.config import PORT
result = probe_kurumi()
result['target'] = kurumi_target_label()
result['PORT'] = PORT
print(json.dumps(result))
" 2>/dev/null || echo '{}')
echo "probe      : $KURUMI_VERDICT"
if ! printf '%s' "$KURUMI_VERDICT" | grep -q '"status": "healthy"'; then
    echo "ACTION REQUIRED (Issue #70): set KURUMI_API_URL in $DST.env to an address the container can reach"
    echo "  unset or self-referencing => the dependency probe reports dependency_not_configured,"
    echo "  and a real Kurumi outage would still be reported as such (no false healthy)."
    echo "  verify with: docker exec fincal python -c \"from app.company_name import probe_kurumi; print(probe_kurumi())\""
fi

echo "=== Service self-report gate (Issue #79) ==="
# Two readings nothing used to check: the deployed commit (the image had no
# VERSION file, so /api/admin/health reported "dev") and the aggregate status
# (the longbridge probe was a host-side tool the image does not ship, so
# `degraded` was permanent and a real outage could not be told apart from it).
# The gate re-derives the aggregate from the probes and exits 1 on a mismatch.
python3 "$DST/scripts/check_service_health.py" --container fincal --expected-version "$COMMIT" || exit 1

echo "=== Identity trust boundary gate (Issue #102) ==="
# The header-identity boundary is fail-closed: AUTH_TRUST_SECRET is only useful
# when the Traefik `fincal-identity` middleware injects the same value, and a
# half-configured pair turns every authenticated request into a 401. Both sides
# are read here (container env + container labels) and one browser session is
# the only thing left to a human — the gate says so explicitly instead of
# shipping a silent 401 wall.
python3 "$DST/scripts/check_auth_trust.py" --container fincal || exit 1

echo "=== Done — fincal $COMMIT deployed and ready ==="
