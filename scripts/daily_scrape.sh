#!/usr/bin/env bash
# Daily scrape for cron — no API server required.
# Example crontab (6 AM UTC daily):
#   0 6 * * * cd /path/to/project && ./scripts/daily_scrape.sh >> /tmp/mileage-daily.log 2>&1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 -m mileage.cli scrape-daily "$@"
