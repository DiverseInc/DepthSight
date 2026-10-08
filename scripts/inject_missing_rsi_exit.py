#!/usr/bin/env python
# File: scripts/inject_missing_rsi_exit.py
"""
Inject the RSI-75 take-profit into strategies that lost it to the template swap.

WHY THIS IS NOT THE CONVERTER
-----------------------------
`restore_legacy_blocks_exits.py` re-converts a row's original `blocks`. That
works only for rows that STILL carry `blocks`. The strategies repaired by the
template swap no longer have them -- the swap overwrote `config_data` with the
template, and the template has no RSI-75 exit. Their original blocks are gone.

So this does the other operation: take a config that is ALREADY in engine format
and that has an RSI-55 entry but no `positionManagement`, and add the missing
exit block. Nothing else in the config is touched.

SCOPE (deliberately narrow)
---------------------------
Targets rows that are ALL of:
  * name LIKE 'RSI Breakout v2%'   -- the template lineage
  * entry child[0] is rsi_condition with value 55
  * `positionManagement` absent

`aa6a6291` "SOL Trend 15m" also has an RSI-55 entry but is a DIFFERENT strategy,
so the name guard excludes it. Do not widen this without checking each row.

SAFETY
------
  * dry-run by default; `--apply` required to write
  * CSV backup of every row before the first write
  * the UPDATE re-checks every condition in its WHERE clause, so the script is
    idempotent and a concurrent hand-edit is not clobbered
  * refuses to write if a row already has `positionManagement`
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

# The block builder is NOT reimplemented here. It is imported from the module
# that owns it and that the test suite drives through the real engine. An
# earlier version of this script carried its own copy with a different _uid
# width, which meant the tested code and the code that writes to the database
# were two different functions. That is exactly the defect the seam tests in
# tests/test_legacy_blocks_config_is_converted.py exist to catch.
from bot_module.legacy_blocks_migration import build_rsi_exit_block

NAME_GUARD = "RSI Breakout v2%"
ENTRY_TYPE = "rsi_condition"
ENTRY_THRESHOLD = "55"
EXIT_THRESHOLD = 75.0
BACKUP_PATH = "/app/logs/rsi_exit_inject_backup.csv"

# Anything not in this set is reported and left alone.
EXPECTED_PREFIXES = [
    "571be7ef",  # RSI Breakout v2              user 2
    "c118a702",  # RSI Breakout v2 (onboarding) user 2
    "37fcdcb0",  # RSI Breakout v2              user 10
    "7c9f4a7e",  # RSI Breakout v2 (onboarding) user 10
    "5b804b4f",  # RSI Breakout v2 (onboarding) user 10
    "6f9cce45",  # RSI Breakout v2 (onboarding) user 10
    "a6438555",  # RSI Breakout v2 (verified)   user 10
]


def select_candidates(conn):
    return conn.fetch(
        """
        SELECT id::text AS id,
               name,
               user_id,
               config_data
          FROM strategy_configs
         WHERE name LIKE $1
           AND config_data::jsonb->'entryConditions'->'children'->0->'type' = $2
           AND config_data::jsonb->'entryConditions'->'children'->0->'params'->>'value' = $3
           AND jsonb_exists(config_data::jsonb, 'positionManagement') = false
         ORDER BY user_id, created_at
        """,
        NAME_GUARD,
        ENTRY_TYPE,
        ENTRY_THRESHOLD,
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument(
        "--allow-unexpected",
        action="store_true",
        help="proceed even if rows match the guard but are not in the expected list",
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

    rows = await select_candidates(conn)
    found = {r["id"][:8] for r in rows}

    print(f"Candidates matching the guard: {len(rows)}\n")
    for r in rows:
        print(f"  {r['id'][:8]}  user {r['user_id']:<3} {r['name'][:44]}")

    missing = set(EXPECTED_PREFIXES) - found
    unexpected = found - set(EXPECTED_PREFIXES)

    if missing:
        print(f"\nWARNING: expected but NOT matched (check the guard): {sorted(missing)}")

    if unexpected:
        print(f"\nNOTE: matched but not in the expected list: {sorted(unexpected)}")
        # Refuse in BOTH modes unless explicitly overridden.
        #
        # This used to be gated on `if not args.apply`, which meant the guard
        # fired in dry-run and then DISAPPEARED under --apply -- i.e. the safety
        # net vanished exactly when it mattered. An unknown row must never be
        # written because the operator added a flag.
        if not args.allow_unexpected:
            print(
                "Refusing to plan unexpected rows. Inspect them first; if they are "
                "genuinely correct targets, re-run with --allow-unexpected."
            )
            await conn.close()
            return 1
        print("Continuing anyway: --allow-unexpected was passed.")

    if not rows:
        print("\nNothing to do.")
        await conn.close()
        return 1

    if not args.apply:
        print(f"\nDRY RUN -- {len(rows)} row(s) would gain an RSI-{int(EXIT_THRESHOLD)} exit.")
        print("Re-run with --apply to write.")
        await conn.close()
        return 0

    os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
    with open(BACKUP_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "name", "user_id", "config_data"])
        for r in rows:
            w.writerow([r["id"], r["name"], r["user_id"], json.dumps(r["config_data"])])
    print(f"\nBackup written to {BACKUP_PATH}")

    applied = 0
    skipped = 0
    for r in rows:
        exit_block = build_rsi_exit_block()
        res = await conn.execute(
            """
            UPDATE strategy_configs
               SET config_data = jsonb_set(config_data::jsonb,
                                          '{positionManagement}', $2::jsonb)
             WHERE id::text = $1
               AND name LIKE $3
               AND config_data::jsonb->'entryConditions'->'children'->0->'params'->>'value' = $4
               AND jsonb_exists(config_data::jsonb, 'positionManagement') = false
            """,
            r["id"],
            json.dumps(exit_block),
            NAME_GUARD,
            ENTRY_THRESHOLD,
        )
        n = int(str(res).split()[-1])
        applied += n
        if n == 0:
            # The row changed under us between SELECT and UPDATE. Nothing was
            # written, but the operator must not read this as success.
            skipped += 1
        print(f"  {r['id'][:8]}  rows_updated={n}{'  <-- SKIPPED (guard no longer matched)' if n == 0 else ''}")

    # verify
    check = await conn.fetch(
        """
        SELECT left(id::text,8) AS id, name,
               jsonb_exists(config_data::jsonb,'positionManagement') AS has_exit
          FROM strategy_configs
         WHERE name LIKE $1
         ORDER BY user_id, created_at
        """,
        NAME_GUARD,
    )
    await conn.close()

    print(f"\nApplied {applied} update(s); {skipped} row(s) skipped by the guard.")
    print("Verification:")
    for c in check:
        print(f"  {c['id']}  has_exit={c['has_exit']}  {c['name'][:40]}")
    print("\nA running bot does NOT reload config from the DB.")
    print("Run:  docker compose restart bot")


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))