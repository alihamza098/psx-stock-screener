#!/usr/bin/env bash
# Start PSX Screener locally (macOS / Linux). Usage: ./start.sh
cd "$(dirname "$0")" || exit 1
: "${PORT:=3100}"
if [ -z "$ADMIN_SECRET" ]; then
  read -rsp "Choose an admin password (used for Refresh / Run research buttons): " ADMIN_SECRET
  echo
fi
export PORT ADMIN_SECRET PSX_OPEN_BROWSER=1
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
exec "$PY" server.py
