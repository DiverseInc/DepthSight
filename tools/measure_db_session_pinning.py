#!/usr/bin/env python3
"""Measure the connection pinning of the controller's DB paths.

Prints, for each scenario, how many sessions were opened and how many
pooled connections were STILL checked out when the controller was done.

The pool is the one the tests use (`tests/test_controller_db_session_leak.py`
fake factory), which checks a connection out when the session is created and
returns it when the session's exit path runs. The factory pins every
generator it hands out ON PURPOSE, so an abandoned generator cannot be
quietly finalised by the garbage collector and make a leak look like a pass.

    python tools/measure_db_session_pinning.py            # this branch
    python tools/measure_db_session_pinning.py --before   # origin/main

`--before` measures the REAL pre-fix code: `bot_module/controller.py` is
replaced with `origin/main`'s version for the duration of the run and
restored afterwards, with the restore verified by SHA-256. Nothing is
simulated - the "before" column is the code that shipped.

The two runs together are the before/after evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("POSTGRES_USER", "testuser")
os.environ.setdefault("POSTGRES_PASSWORD", "testpassword")
os.environ.setdefault("POSTGRES_DB", "testdb")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("RATELIMIT_ENABLED", "false")

# The swap has to happen BEFORE `bot_module.controller` is imported, so the
# "before" run really executes origin/main's controller.
_ap = argparse.ArgumentParser()
_ap.add_argument(
    "--before",
    action="store_true",
    help="measure origin/main's controller instead of this branch's",
)
_ap.add_argument(
    "--before-from",
    metavar="PATH",
    help=(
        "measure the pre-fix controller from this file instead of asking git "
        "for it. Needed when the worktree is a linked worktree whose git "
        "directory is not reachable from the interpreter's environment."
    ),
)
_args = _ap.parse_args()

_CONTROLLER = REPO / "bot_module" / "controller.py"
_original: bytes | None = None
_digest: str | None = None


def _pre_fix_controller() -> bytes:
    if _args.before_from:
        return Path(_args.before_from).read_bytes()
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "-c",
                "safe.directory=*",
                "show",
                "origin/main:bot_module/controller.py",
            ],
            capture_output=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            "could not read origin/main's controller.py with git "
            f"({exc}).\nExport it first and use --before-from:\n"
            "    git show origin/main:bot_module/controller.py "
            "> /tmp/controller_main.py\n"
            "    python tools/measure_db_session_pinning.py --before "
            "--before-from /tmp/controller_main.py"
        )


if _args.before:
    _original = _CONTROLLER.read_bytes()
    _digest = hashlib.sha256(_original).hexdigest()
    _pre_fix = _pre_fix_controller()
    _CONTROLLER.write_bytes(_pre_fix)
    print(f"[before] swapped in origin/main's controller.py ({len(_pre_fix)} bytes)")


from bot_module.runtime_dependencies import crud  # noqa: E402
from tests.test_controller_db_session_leak import (  # noqa: E402
    _DbFactory,
    _controller,
)


async def _scenario_one_read(crud_module):
    async def _fake(db, user_id):
        return None

    crud_module.get_user_symbol_selection_config = _fake
    factory = _DbFactory()
    ctrl = _controller(factory)
    await ctrl.load_symbol_selection_config()
    return factory


async def _scenario_failed_read(crud_module):
    async def _boom(db, user_id):
        raise RuntimeError("db is down")

    crud_module.get_user_symbol_selection_config = _boom
    factory = _DbFactory()
    ctrl = _controller(factory)
    await ctrl.load_symbol_selection_config()
    return factory


async def _scenario_adopted_position(crud_module):
    async def _fake(db, user_id, symbol):
        return SimpleNamespace(
            strategy_config=SimpleNamespace(name="VolumeBreakout"),
            strategy_config_id=7,
            trade_uuid="x-entry-abc",
        )

    crud_module.get_last_open_trade_for_symbol = _fake
    factory = _DbFactory()
    ctrl = _controller(factory)
    executor = MagicMock()
    executor.get_open_orders = AsyncMock(return_value=[])
    await ctrl._build_adopted_position(
        "BTCUSDT",
        {"positionAmt": "1", "entryPrice": "100"},
        "futures_usdtm",
        executor,
    )
    return factory


async def _scenario_fifty_operations(crud_module):
    async def _fake_selection(db, user_id):
        return None

    async def _fake_config(db, user_id=None):
        return None

    crud_module.get_user_symbol_selection_config = _fake_selection
    crud_module.get_config = _fake_config
    factory = _DbFactory()
    ctrl = _controller(factory)
    for _ in range(50):
        await ctrl.load_symbol_selection_config()
        await ctrl.reload_user_app_config()
    return factory


SCENARIOS = (
    ("one read", _scenario_one_read),
    ("a FAILED read", _scenario_failed_read),
    ("the adopted-position lookup", _scenario_adopted_position),
    ("50 controller operations", _scenario_fifty_operations),
)


async def main() -> int:
    # The scenarios each install the crud functions they need, right before
    # they run, so they cannot leak into one another.
    print(f"{'scenario':<32} {'opened':>7} {'pinned':>7}   result")
    print("-" * 66)
    failures = 0
    for label, scenario in SCENARIOS:
        factory = await scenario(crud)
        pinned = factory.pool.checked_out
        verdict = "LEAK" if pinned else "clean"
        if pinned:
            failures += 1
        print(f"{label:<32} {factory.pool.opened:>7} {pinned:>7}   {verdict}")
    print("-" * 66)
    print(
        f"{'TOTAL PINNED':<32} {'':>7} {failures:>7}   "
        f"{'LEAK' if failures else 'clean'}"
    )
    return 0


def _restore() -> int:
    if _original is None or _digest is None:
        return 0
    _CONTROLLER.write_bytes(_original)
    ok = hashlib.sha256(_CONTROLLER.read_bytes()).hexdigest() == _digest
    print(f"[before] restored controller.py, sha256 match: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        code = _restore() or code
    sys.exit(code)
