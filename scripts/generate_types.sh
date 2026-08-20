#!/usr/bin/env bash
# Generate TypeScript types from FastAPI OpenAPI schema.
# Usage: bash scripts/generate_types.sh [--check]
#
# --check: only verify types are up-to-date (for CI), exit 1 if stale.
set -euo pipefail

cd "$(dirname "$0")/.."

OPENAPI_URL="${FINCAL_OPENAPI_URL:-http://localhost:8000/openapi.json}"
OUT_FILE="frontend/src/api/schema.d.ts"

# Ensure output directory exists
mkdir -p "$(dirname "$OUT_FILE")"

if [ "${1:-}" = "--check" ]; then
    echo "=== Checking TypeScript types are up-to-date ==="
    npx openapi-typescript "$OPENAPI_URL" -o "$OUT_FILE.check"
    if diff -q "$OUT_FILE" "$OUT_FILE.check" > /dev/null 2>&1; then
        echo "OK: types are in sync"
        rm "$OUT_FILE.check"
        exit 0
    else
        echo "FAIL: types are stale. Run 'bash scripts/generate_types.sh' to update."
        diff --color "$OUT_FILE" "$OUT_FILE.check" || true
        rm "$OUT_FILE.check"
        exit 1
    fi
fi

echo "=== Generating TypeScript types from $OPENAPI_URL ==="
npx openapi-typescript "$OPENAPI_URL" -o "$OUT_FILE"

echo "Generated: $OUT_FILE ($(wc -l < "$OUT_FILE") lines)"
