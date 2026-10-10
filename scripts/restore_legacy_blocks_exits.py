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

    # 1. always dry-run first (nothing is written without --apply)
    docker compose exec -T bot python scripts/restore_legacy_blocks_exits.py \
      --ids be485ab9-384a-4adb-9f9b-a2df412d6db0 --sl-atr 1.5 --tp-rr 2.0

    # 2. only after reviewing the dry-run output
    docker compose exec -T bot python scripts/restore_legacy_blocks_exits.py \
      --ids be485ab9-384a-4adb-9f9b-a2df412d6db0 --sl-atr 1.5 --tp-rr 2.0 --apply

--sl-atr and --tp-rr are REQUIRED and have no defaults. Legacy blocks carry no
stop-loss or take-profit, so any value supplied here introduces a risk
parameter that did not previously exist. That is an operator decision.
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

BACKUP_PATH = "/app/logs/blocks_restore_backup.csv"

# NOTE: this script previously carried a hardcoded TARGET_IDS list of
# PLACEHOLDER uuids that matched no real row. They were flagged as
# placeholders rather than fabricated into plausible-looking ids. Targets are
# now passed explicitly with --ids so a wrong id can never look real.
#
# Verified live 2026-10-08 (16-row audit) -- still legacy `blocks`:
#   be485ab9-384a-4adb-9f9b-a2df412d6db0  paper-test-2       BTCUSDT 1h  RSI 55/75  converts cleanly
#   e43d1cb2-123c-4ca7-ba4f-bb25d245e969  paper-bollinger-1  ETHUSDT 15m Bollinger    REFUSED (reaches_middle lossy)
#   e898f74e                              EMA 50/200 Golden Cross 4h               see audit note


def _dsn():
    return (
        f"postgresql+asyncpg://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:"
        f"{os.environ.get('POSTGRES_PORT', 5432)}/{os.environ['POSTGRES_DB']}"
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ids",
        nargs="+",
        required=True,
        help="full config ids (uuids) to convert. Explicit on purpose: a wrong "
        "id must never be able to look like a real one.",
    )
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument(
        "--sl-atr",
        type=float,
        required=True,
        help="stop-loss ATR multiple. REQUIRED, no default: legacy blocks carry no "
        "stop-loss, so any value here is a risk decision that belongs to the "
        "operator, not to this script.",
    )
    ap.add_argument(
        "--tp-rr",
        type=float,
        required=True,
        help="take-profit reward:risk. REQUIRED, no default, for the same reason "
        "as --sl-atr.",
    )
    args = ap.parse_args()

    if args.sl_atr <= 0 or args.tp_rr <= 0:
        print("--sl-atr and --tp-rr must both be > 0")
        return 1

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
        list(args.ids),
    )
    if not rows:
        print("No matching strategies found. Check the ids against the live DB.")
        await conn.close()
        return 1

    print(f"Found {len(rows)} target strategies.")
    print(f"Risk parameters: sl_atr={args.sl_atr}  tp_rr={args.tp_rr}")
    print("(these are NOT carried in legacy blocks - they are being introduced)\n")

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
        planned.append(
            (row["id"], row["name"], converted, has_exit, json.dumps(cfg))
        )

        print(f"  {row['id'][:8]}  {row['name'][:40]:<40} exit_restored={has_exit}")

    if skipped:
        print("\nSkipped:")
        for sid, why in skipped:
            print(f"  {sid[:8]}  {why}")

    missing = [p for p in planned if not p[3]]
    if missing:
        print(f"\nWARNING: {len(missing)} row(s) would be written WITHOUT an exit:")
        for p in missing:
            print(f"  {p[0][:8]}  {p[1]}")

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
    applied, contended = 0, 0
    for sid, _name, converted, _has_exit, original_cfg in planned:
        # Compare-and-swap. The jsonb_exists check alone only proves a key named
        # 'blocks' is present; it does not prove the row is still the row we
        # read. A concurrent hand-edit that keeps the key would pass that guard
        # and be overwritten by a conversion of data we no longer hold. The
        # equality predicate closes that window: if anything changed since the
        # SELECT, the UPDATE matches zero rows and nothing is written.
        res = await conn.execute(
            """
            UPDATE strategy_configs
               SET config_data = $3::jsonb
             WHERE id::text = $1
               AND jsonb_exists(config_data::jsonb, 'blocks')
               AND config_data::jsonb = $2::jsonb
            """,
            sid,
            original_cfg,
            json.dumps(converted),
        )
        n = int(res.split()[-1]) if res else 0
        applied += n
        if n == 0:
            contended += 1
            print(f"  {sid[:8]}  rows_updated=0  (row changed since it was read -- SKIPPED)")
        else:
            print(f"  {sid[:8]}  rows_updated={n}")

    await conn.close()
    print(f"\nApplied {applied} update(s).")
    if contended:
        print(
            f"WARNING: {contended} row(s) changed between the read and the write and "
            f"were left untouched. Re-run the dry-run to see their current state."
        )
    if applied == 0:
        print("Nothing was written -- the guard blocked every row. Inspect above.")
    else:
        print("A running bot instance does NOT reload config from the DB.")
        print("Run:  docker compose restart bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))