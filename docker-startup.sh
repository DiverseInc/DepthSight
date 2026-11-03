#!/bin/bash
set -e

LOCK_FILE="/tmp/migrations.lock"

echo "Waiting for migration lock..."

# Running alembic upgrade head.
echo "Running migrations..."
if flock -x "$LOCK_FILE" -c "alembic upgrade head"; then
    echo "Migrations check completed successfully."
else
    migration_status=$?
    echo "CRITICAL: the database migration FAILED (the 'alembic upgrade head' step exit code ${migration_status})." >&2
    echo "CRITICAL: the schema may be missing tables or columns. The application is starting anyway." >&2
    echo "CRITICAL: check the migration output above, fix the migration, and restart this container." >&2
fi


echo "Starting application..."
exec "$@"
