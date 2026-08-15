#!/bin/bash
# ============================================================
# Daily postgres backup for DepthSight.
#
# - pg_dump the depthsight_db
# - gzip + timestamped filename
# - keep last 7 days locally
# - copies to /backups (a host-mounted volume) so Elestio can
#   snapshot it with the rest of the host
# - writes a tiny "last_backup.json" manifest you can inspect
# ============================================================
set -euo pipefail

BACKUP_DIR=/backups
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date -u +%Y%m%d-%H%M%SZ)
FILENAME="depthsight-${TIMESTAMP}.sql.gz"
TMP="/tmp/${FILENAME}"
FINAL="${BACKUP_DIR}/${FILENAME}"

STATE_DIR=/opt/ops/state
mkdir -p "$STATE_DIR"
MANIFEST="$STATE_DIR/last_backup.json"

# pg_dump via the postgres container
echo "[*] dumping database..."
if ! PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "${POSTGRES_HOST:-postgres}" \
        -U "${POSTGRES_USER:-depthsight_user}" \
        -d "${POSTGRES_DB:-depthsight_db}" \
        --no-owner --no-acl \
        | gzip > "$TMP"; then
    echo "[!] pg_dump failed" >&2
    exit 1
fi

# Sanity: non-zero file
SIZE=$(stat -c%s "$TMP" 2>/dev/null || stat -f%z "$TMP")
if [ "${SIZE:-0}" -lt 1024 ]; then
    echo "[!] backup too small ($SIZE bytes) - likely empty" >&2
    exit 1
fi

mv "$TMP" "$FINAL"

# Retention: 7 days
DELETED=$(find "$BACKUP_DIR" -name "depthsight-*.sql.gz" -mtime +7 -delete -print | wc -l)
REMAINING=$(find "$BACKUP_DIR" -name "depthsight-*.sql.gz" | wc -l)

# Manifest
cat > "$MANIFEST" <<EOF
{
  "last_backup": "$FILENAME",
  "size_bytes": $SIZE,
  "deleted_old": $DELETED,
  "remaining": $REMAINING,
  "ran_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo "[+] backup complete: $FILENAME ($SIZE bytes), $REMAINING kept, $DELETED pruned"
