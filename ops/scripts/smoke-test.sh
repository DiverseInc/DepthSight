#!/bin/bash
# ============================================================
# Smoke test wrapper for the cron container.
#
# Talks to postgres + the api over the docker network, not via
# docker socket, so we don't need to mount /var/run/docker.sock.
#
# On failure, writes /opt/ops/state/alert that you can ship to
# Slack/email/PagerDuty via a separate channel, or just curl
# the file from outside.
# ============================================================
set -u

STATE_DIR="${STATE_DIR:-/opt/ops/state}"
ALERT_FILE="$STATE_DIR/alert"
LOG_FILE="$STATE_DIR/smoke.log"

mkdir -p "$STATE_DIR"

DOMAIN="${PUBLIC_BASE_URL:-https://depthsight.diverseinc.net}"
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_DB="${POSTGRES_DB:-depthsight_db}"
PG_USER="${POSTGRES_USER:-depthsight_user}"
API_URL="$DOMAIN/api/v1"

FAIL=0
{
    echo "=== smoke test @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

    # 1. Postgres reachable + roles sane
    if PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" \
            -c "SELECT 1 FROM users LIMIT 1;" >/dev/null 2>&1; then
        echo "  [ok] postgres reachable, users table queryable"
    else
        echo "  [FAIL] postgres unreachable or users table missing"
        FAIL=1
    fi

    # 2. API health endpoint (whatever the api exposes for /health)
    HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "$DOMAIN" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ]; then
        echo "  [ok] GET $DOMAIN -> $HTTP_CODE"
    else
        echo "  [FAIL] GET $DOMAIN -> $HTTP_CODE"
        FAIL=1
    fi

    # 3. WebSocket upgrade response (404/426/400/101 = expected for plain curl,
    #    because ws servers reject plain HTTP with one of these. Only fail on
    #    connection errors (000) or 5xx server errors.)
    WS_CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "$DOMAIN/ws" 2>/dev/null || echo "000")
    case "$WS_CODE" in
        404|426|400|101)
            echo "  [ok] GET $DOMAIN/ws -> $WS_CODE (ws endpoint reachable)"
            ;;
        5*)
            echo "  [FAIL] GET $DOMAIN/ws -> $WS_CODE (server error)"
            FAIL=1
            ;;
        000)
            echo "  [FAIL] GET $DOMAIN/ws -> connection failed"
            FAIL=1
            ;;
        *)
            echo "  [FAIL] GET $DOMAIN/ws -> $WS_CODE (unexpected)"
            FAIL=1
            ;;
    esac

    # 4. .env sanity (if mounted)
    if [ -f /opt/depthsight/.env ]; then
        if grep -q '[{}]' /opt/depthsight/.env; then
            echo "  [FAIL] .env contains { or } characters - elestio .env bug regressed!"
            FAIL=1
        else
            echo "  [ok] .env is clean (no { or })"
        fi
    fi

    # 5. The AGPL-3.0 section 13 source offer must be reachable.
    #
    # A 200 on /source is NOT enough: when the Caddy route is missing the
    # path falls through to the React SPA catch-all, which also answers
    # 200 with the app shell. The BODY is therefore checked for the
    # repository address the offer has to carry, so a broken route is
    # reported instead of looking healthy.
    SOURCE_CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "$DOMAIN/source" 2>/dev/null || echo "000")
    if [ "$SOURCE_CODE" != "200" ]; then
        echo "  [FAIL] GET $DOMAIN/source -> $SOURCE_CODE (source offer unreachable)"
        FAIL=1
    elif curl -sk --max-time 10 "$DOMAIN/source" 2>/dev/null | grep -q "DiverseInc/DepthSight"; then
        echo "  [ok] GET $DOMAIN/source -> 200 (source offer served)"
    else
        echo "  [FAIL] GET $DOMAIN/source -> 200 but the source offer was not served"
        FAIL=1
    fi

    # This whole block is piped into tee, so it runs in a SUBSHELL and a
    # FAIL=1 set inside it never reaches the parent shell -- the alert
    # file below could never be written. The exit status is what carries
    # the failure out of the pipeline.
    exit "$FAIL"
} 2>&1 | tee -a "$LOG_FILE"
SMOKE_RC=${PIPESTATUS[0]}

# Rotate log if it gets big (>1MB)
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    mv "$LOG_FILE" "$LOG_FILE.old"
fi

if [ "$SMOKE_RC" -ne 0 ]; then
    {
        echo "=== ALERT: smoke test FAILED at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
        tail -20 "$LOG_FILE"
        echo "=== /ALERT ==="
    } > "$ALERT_FILE"
    exit 1
else
    # Clear any old alert
    rm -f "$ALERT_FILE"
    exit 0
fi
