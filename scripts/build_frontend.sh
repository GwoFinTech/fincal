#!/usr/bin/env bash
# Regenerate the FinCal frontend static assets (Issue #42).
#
# Produces / consumes, under the repo root:
#   app/static/assets/tailwind.css            — prerendered Tailwind CSS
#   app/static/assets/vendor/vue.global.prod.js — self-hosted Vue 3 prod build
#
# The committed artifacts are the source of truth for deployments: deploy.sh
# rsyncs the repo (including these files) into /opt/fincal and the Docker image
# COPYs `app/`. No node is required at deploy/runtime — this script exists only
# so maintainers can rebuild after changing markup. Version pins are fixed to
# avoid third-party drift.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${TMPDIR:-/tmp}/fincal-build"
VUE_VERSION="3.4.38"
TAILWIND_VERSION="3.4.17"

echo "=== FinCal frontend build (Issue #42) ==="
mkdir -p "$BUILD"
cd "$BUILD"

# Bootstrap tailwindcss (only needed to regenerate; absent at deploy).
if [ ! -x ./node_modules/.bin/tailwindcss ]; then
  npm init -y >/dev/null 2>&1
  npm install tailwindcss@"$TAILWIND_VERSION" autoprefixer >/dev/null 2>&1
fi

echo "--- Tailwind: $ROOT/app/static/index.html + app-setup.js → app/static/assets/tailwind.css ---"
./node_modules/.bin/tailwindcss \
  -c "$ROOT/frontend/tailwind.config.js" \
  -i "$ROOT/frontend/input.css" \
  -o "$ROOT/app/static/assets/tailwind.css" \
  --minify

echo "--- Vendor Vue $VUE_VERSION → app/static/assets/vendor/vue.global.prod.js ---"
mkdir -p "$ROOT/app/static/assets/vendor"
curl -fsSL -o "$ROOT/app/static/assets/vendor/vue.global.prod.js" \
  "https://unpkg.com/vue@${VUE_VERSION}/dist/vue.global.prod.js"

echo "Done. Verify:"
echo "  grep -c 'unpkg.com|cnd.tailwindcss.com' $ROOT/app/static/index.html  # must be 0"
ls -la "$ROOT/app/static/assets/tailwind.css" "$ROOT/app/static/assets/vendor/vue.global.prod.js"
