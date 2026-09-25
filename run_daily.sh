#!/bin/bash
# Daily refresh: activates the venv and runs the fetch/screen engine.
# Scheduled via launchd (see com.manav.tradingframework.plist) or cron.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Activate venv if present.
if [ -f "$DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$DIR/.venv/bin/activate"
fi

echo "[$(date)] Starting daily NSE screen..."
python data_fetch.py "$@"
echo "[$(date)] Done. DB: $DIR/data/screener.db"
