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
# The live environment file. The ops container mounts it read-only at this
# path, so it always holds the CURRENT stamp -- unlike the container's own
# environment, which is fixed when the container is created. It is
# overridable so the smoke test can be exercised against a fixture.
ENV_FILE="${DEPLOY_ENV_FILE:-/opt/depthsight/.env}"
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_DB="${POSTGRES_DB:-depthsight_db}"
PG_USER="${POSTGRES_USER:-depthsight_user}"
API_URL="$DOMAIN/api/v1"

FAIL=0

# The source offer is a PUBLIC document, so it has to be served over a
# certificate that verifies -- the liveness checks below use `curl -k` on
# purpose, but this one must not.
#
# curl's exit codes are DISTINCT: 60 is "the certificate does not verify",
# 7 is "connection refused", 28 is "timeout". ONLY 60 is a certificate
# failure. So the status is CAPTURED, and it DECIDES the message: reporting
# every failure as a certificate failure sends the operator to fix a
# certificate that is perfectly fine.
#
# The status is captured in the ELSE branch of a plain `if curl`: inside
# `if ! cmd` the `$?` is the status of the NEGATION (0), not of the command,
# so the negation form cannot report WHICH failure it was.
#
# The request is retried a BOUNDED three times, with a pause between the
# attempts, exactly like the sibling checks in update.sh: this smoke test
# runs every five minutes from ops/loop.sh, and caddy may still be coming
# up when it fires.
#
# It is a FUNCTION so that the REAL logic is what the tests exercise
# (AGENTS.md: "Verify against the real function, not a reimplementation").
# It sets the smoke test's own FAIL counter instead of exiting, so the
# remaining checks still run and the alert file is still written.
check_offer_certificate() {
    local attempt status
    status=0
    for attempt in 1 2 3; do
        if curl -s -o /dev/null --max-time 10 "$DOMAIN/source" 2>/dev/null; then
            return 0
        else
            status=$?
        fi
        if [ "$attempt" -lt 3 ]; then
            sleep 5
        fi
    done

    if [ "$status" -eq 60 ]; then
        echo "  [FAIL] GET $DOMAIN/source -> the TLS certificate did not verify"
    else
        echo "  [FAIL] GET $DOMAIN/source -> the site did not answer (curl exit status $status, which is not a certificate failure)"
    fi
    FAIL=1
    return 1
}

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
    if [ -f "$ENV_FILE" ]; then
        if grep -q '[{}]' "$ENV_FILE"; then
            echo "  [FAIL] .env contains { or } characters - elestio .env bug regressed!"
            FAIL=1
        else
            echo "  [ok] .env is clean (no { or })"
        fi
    fi

    # 5. The AGPL-3.0 section 13 source offer must be reachable AND served
    #    over a certificate that verifies.
    #
    #    A 200 on /source is NOT enough: when the Caddy route is missing the
    #    path falls through to the React SPA catch-all, which also answers
    #    200 with the app shell. The BODY is therefore checked for the
    #    repository address the offer has to carry, so a broken route is
    #    reported instead of looking healthy.
    #
    #    The liveness checks above use `curl -k` ON PURPOSE: they ask "is the
    #    server answering", and a certificate problem must not make a healthy
    #    site look dead. This check asks a different question -- "is the PUBLIC
    #    offer being served properly" -- and a check that skips certificate
    #    verification cannot tell the real offer from one served through an
    #    impersonated certificate. So this one verifies, and a certificate
    #    failure is reported as itself rather than as "unreachable".
    #
    #    The verification itself is the FUNCTION check_offer_certificate
    #    (above), because the exit status of curl has to be READ to tell a
    #    certificate failure (60) from a site that is not answering (7, 28)
    #    -- and inside `if ! curl` that status cannot be read at all.
    if check_offer_certificate; then
        SOURCE_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$DOMAIN/source" 2>/dev/null || echo "000")
        if [ "$SOURCE_CODE" != "200" ]; then
            echo "  [FAIL] GET $DOMAIN/source -> $SOURCE_CODE (source offer unreachable)"
            FAIL=1
        elif curl -s --max-time 10 "$DOMAIN/source" 2>/dev/null | grep -q "DiverseInc/DepthSight"; then
            echo "  [ok] GET $DOMAIN/source -> 200 (source offer served over a verified certificate)"
        else
            echo "  [FAIL] GET $DOMAIN/source -> 200 but the source offer was not served"
            FAIL=1
        fi
    fi

    # 6. The AGPL offer PROMISES a downloadable archive of the exact tree of
    #    the running build, and until now nothing ever FETCHED it: a 404
    #    archive or an empty archive passed every check, because only the
    #    PAGE was checked.
    #
    #    The commit is the STAMPED one -- the same DEPLOYED_COMMIT the api
    #    publishes as the `commit` field of GET /api/v1/status. It is read
    #    from the mounted .env (live, never stale) and from the environment
    #    as a fallback. A deployment from before the stamp existed has no
    #    commit: the check is SKIPPED, because there is nothing to check.
    #    A stamped commit whose archive does not resolve FAILS: the offer
    #    is naming a file that is not there.
    STAMPED_COMMIT=""
    STAMP_KEY="DEPLOYED_COMMIT"
    if [ -f "$ENV_FILE" ]; then
        STAMPED_COMMIT=$(grep -E "^${STAMP_KEY}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | tr -d '"' || true)
    fi
    if [ -z "$STAMPED_COMMIT" ]; then
        STAMPED_COMMIT="${DEPLOYED_COMMIT:-}"
    fi
    if [ -z "$STAMPED_COMMIT" ]; then
        echo "  [ok] SKIP the source archive check: no DEPLOYED_COMMIT is stamped"
    else
        ARCHIVE_URL="https://github.com/DiverseInc/DepthSight/archive/${STAMPED_COMMIT}.tar.gz"
        ARCHIVE_BODY=$(mktemp)
        ARCHIVE_CODE=$(curl -sL -o "$ARCHIVE_BODY" -w "%{http_code}" --max-time 30 "$ARCHIVE_URL" 2>/dev/null || echo "000")
        if [ "$ARCHIVE_CODE" != "200" ]; then
            echo "  [FAIL] GET $ARCHIVE_URL -> $ARCHIVE_CODE (the source archive the offer names is not available)"
            FAIL=1
        elif [ ! -s "$ARCHIVE_BODY" ]; then
            echo "  [FAIL] GET $ARCHIVE_URL -> 200 but the source archive is empty"
            FAIL=1
        else
            echo "  [ok] GET $ARCHIVE_URL -> 200 (the source archive the offer names is available)"
        fi
        rm -f "$ARCHIVE_BODY"
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
