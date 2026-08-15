# ops/ — DepthSight ops sidecar

A small container that runs two scheduled jobs:

1. **Smoke test** every 5 minutes — checks the api, websocket, postgres, and the
   .env file (catches the Elestio .env corruption regression automatically).
2. **Postgres backup** daily at 02:30 UTC — pg_dump + gzip, 7-day retention,
   written to the `backups` volume (host-mounted by Elestio for snapshotting).

## How it works

- Built from `ops/Dockerfile` (alpine + bash + postgresql-client + curl + jq).
- Runs a simple bash scheduler loop (`ops/loop.sh`) in the foreground as PID 1.
- The loop checks the time every 30s and runs jobs when their schedule matches.
- Scripts in `ops/scripts/`.

## Deploy

```bash
cd /opt/app/depthsight
docker compose build ops
docker compose up -d ops
```

The first build takes ~30s (small alpine base + a few apk installs + supercronic binary).

## Check status

```bash
# Confirm the container is up
docker compose ps ops

# Tail recent smoke test output
docker exec depthsight_ops cat /opt/ops/state/smoke.log | tail -20

# Check the last backup
docker exec depthsight_ops cat /opt/ops/state/last_backup.json

# See if there's an active alert
docker exec depthsight_ops ls -la /opt/ops/state/alert 2>/dev/null || echo "no active alert"
```

## Alerting (manual)

If `/opt/ops/state/alert` exists, the smoke test is failing. Wire this up to Slack/Telegram/email:

```bash
# Example: Telegram bot
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d chat_id="${TELEGRAM_CHAT_ID}" \
  -d text="$(cat /opt/ops/state/alert)"
```

Add a 1-min cron in `ops/crontab` to ship alerts to your channel of choice.

## Files

- `Dockerfile` — alpine + bash + postgresql-client
- `loop.sh` — the scheduler (runs as PID 1)
- `scripts/smoke-test.sh` — runs the smoke checks, writes alert on failure
- `scripts/backup.sh` — pg_dump + retention
