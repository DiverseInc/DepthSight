#!/bin/bash
# ============================================================
# Simple scheduler loop. Replaces supercronic (which had exec
# issues in this alpine image). Runs in the foreground as
# PID 1 so Docker can manage the lifecycle.
#
# Schedule:
#   - Every 5 min: smoke test
#   - Daily at 02:30 UTC: pg_dump backup
#   - Every loop tick (60s): re-evaluate time-based jobs
# ============================================================
set -u

cd /opt/app/depthsight 2>/dev/null || cd /opt/depthsight 2>/dev/null || true

LAST_SMOKE_TS=0
LAST_BACKUP_DAY=""

echo "[loop] starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

while true; do
    NOW_TS=$(date +%s)
    DAY=$(date -u +%Y%m%d)
    HOUR=$(date -u +%H)
    MINUTE=$(date -u +%M)

    # Smoke test every 5 minutes
    if [ $((NOW_TS - LAST_SMOKE_TS)) -ge 300 ]; then
        /opt/ops/smoke-test.sh
        LAST_SMOKE_TS=$NOW_TS
    fi

    # Daily backup at 02:30 UTC (only run once per day)
    if [ "$HOUR" = "02" ] && [ "$MINUTE" = "30" ] && [ "$LAST_BACKUP_DAY" != "$DAY" ]; then
        /opt/ops/backup.sh
        LAST_BACKUP_DAY="$DAY"
    fi

    sleep 30
done
