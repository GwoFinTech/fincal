#!/usr/bin/env bash
# Sync fincal from src-aigen (dev) → /opt (deploy), then rebuild.
set -euo pipefail
SRC="/root/src-aigen/fincal/"
DST="/opt/fincal/"

echo "=== Syncing fincal: $SRC → $DST ==="
rsync -av --delete \
  --exclude='.env' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$SRC" "$DST"

echo "=== Rebuilding & deploying ==="
cd "$DST"
docker compose build --no-cache 2>&1 | tail -3
docker compose up -d 2>&1

echo "=== Done ==="
