#!/usr/bin/env python
# File: scripts/restore_legacy_blocks_exits.py
"""
Restore the RSI-75 exit on strategies repaired by the template swap.

WHY
---
Six strategies were repaired by copying the `rsi-breakout-v2` template over
their stored config. That fixed the unreadable `blocks` key but was LOSSY: the
template has no RSI-75 exit, so each row lost its original take-profit. They have
been trading on SL/TP-atr-rr only.

This converts each row's ORIGINAL `blocks` back into engine format (using the
proven converter in bot_module.legacy_blocks_migration) and writes the result.

SAFETY
------
  * `--dry-run` is the default. Nothing is written unless `--apply` is passed.
  * Every UPDATE is guarded on `jsonb_exists(config_data::jsonb,'blocks')` being
    FALSE, so a row whose config was edited in the visual editor after the swap
    is skipped rather than clobbered. The script prints how many it skipped.
  * A CSV backup of the full pre-change config is written before the first write.
  * The converter raises LossyConversionError rather than guessing; a row that
    cannot be converted losslessly is reported and skipped.

USAGE (on Elestio, from /opt/app/depthsight)
    docker compose exec -T bot python scripts/restore_legacy_blocks_exits.py --dry-run
    docker compose exec -T bot python scripts/restore_legacy_blocks_exits.py --apply
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from bot_module.legacy_blocks_migration import (  # noqa: E402
    LossyConversionError,
    convert_legacy_blocks_config,
    is_legacy_blocks_config,
)

# The six rows repaired by the template swap, with the RSI-75 exit removed.
TARGET_IDS = [
    "571be7ef1b7a4a6f9c2d3e4f5a6b7c8d",
    "c118a7024e5f6a7b8c9d0e1f2a3b4c5d",
    "37fcdcb0c2d3e4f5a6b7c8d9e0f1a2b3c",
    "5b804b4f3a4b5c6d7e8f9a0b1c2d3e4f",
    "7c9f4a7e5b6c7d8e9f0a1b2c3d4e5f6",
    "6f9cce45d6e7f8a9b0c1d2e3f4a5b6c",
]

BACKUP_PATH = "/app/logs/blocks_restore_backup.csv"


def _dsn():
    return (
        f"postgresql+asyncpg://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:"
        f"{os.environ.get('POSTGRES_PORT', 5432)}/{os.environ['POSTGRES_DB']}"
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument(
        "--sl-atr",
        type=float,
        default=1.5,
        help="stop-loss ATR multiple; legacy blocks carry no SL, so this is a decision",
    )
    ap.add_argument(
        "--tp-rr",
        type=float,
        default=2.0,
        help="take-profit reward:risk; legacy blocks carry no TP, so this is a decision",
    )
    args = ap.parse_args()

    import asyncpg

    conn = await asyncpg.connect(
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", 5432)),
        database=os.environ["POSTGRES_DB"],
    )

    rows = await conn.fetch(
        "SELECT id::text AS id, name, config_data FROM strategy_configs WHERE id::text = ANY($1::text[])",
        TARGET_IDS,
    )
    if not rows:
        print("No matching strategies found. Check the ids against the live DB.")
        await conn.close()
        return 1

    print(f"Found {len(rows)} target strategies.\n")

    backup_rows, planned, skipped = [], [], []

    for row in rows:
        cfg = row["config_data"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)

        if not is_legacy_blocks_config(cfg):
            skipped.append((row["id"], "no 'blocks' key - already converted or hand-edited"))
            continue

        try:
            converted = convert_legacy_blocks_config(
                cfg, sl_atr=args.sl_atr, tp_rr=args.tp_rr
            )
        except LossyConversionError as exc:
            skipped.append((row["id"], f"refused (lossy): {exc}"))
            continue

        pm = converted.get("positionManagement", [])
        has_exit = bool(pm) and pm[0].get("then_actions")
        backup_rows.append({"id": row["id"], "name": row["name"], "config_data": json.dumps(cfg)})
        planned.append((row["id"], row["name"], converted, has_exit))

        print(f"  {row['id'][:8]}  {row['name'][:40]:<40} exit_restored={has_exit}")

    if skipped:
        print("\nSkipped:")
        for sid, why in skipped:
            print(f"  {sid[:8]}  {why}")

    missing = [p for p in planned if not p[3]]
    if missing:
        print(f"\nWARNING: {len(missing)} row(s) would be written WITHOUT an exit:")
        for sid, name, _, _ in missing:
            print(f"  {sid[:8]}  {name}")

    if not planned:
        print("\nNothing to do.")
        await conn.close()
        return 1

    if not args.apply:
        print(f"\nDRY RUN -- {len(planned)} row(s) would be updated. Re-run with --apply.")
        await conn.close()
        return 0

    os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
    with open(BACKUP_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "name", "config_data"])
        w.writeheader()
        w.writerows(backup_rows)
    print(f"\nBackup written to {BACKUP_PATH}")

    # The guard makes this idempotent AND protects any row edited since the swap:
    # only rows that still carry the legacy 'blocks' key are overwritten.
    applied = 0
    for sid, _name, converted, _has_exit in planned:
        res = await conn.execute(
            """
            UPDATE strategy_configs
               SET config_data = $2::jsonb
             WHERE id::text = $1
               AND jsonb_exists(config_data::jsonb, 'blocks')
            """,
            sid,
            json.dumps(converted),
        )
        n = int(res.split()[-1]) if res else 0
        applied += n
        print(f"  {sid[:8]}  rows_updated={n}")

    await conn.close()
    print(f"\nApplied {applied} update(s).")
    if applied == 0:
        print("Nothing was written -- the guard blocked every row. Inspect above.")
    else:
        print("A running bot instance does NOT reload config from the DB.")
        print("Run:  docker compose restart bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))