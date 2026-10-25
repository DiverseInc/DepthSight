#!/usr/bin/env python3
"""Measure the connection pinning of the controller's DB paths.

Prints, for each scenario, how many sessions were opened and how many
pooled connections were STILL checked out when the controller was done.

The pool is the one the tests use (`tests/test_controller_db_session_leak.py`
fake factory), which checks a connection out when the session is created and
returns it when the session's exit path runs. The factory pins every
generator it hands out ON PURPOSE, so an abandoned generator cannot be
quietly finalised by the garbage collector and make a leak look like a pass.

    python tools/measure_db_session_pinning.py            # the fixed tree
    python tools/measure_db_session_pinning.py --leaky    # the pre-fix shape

`--leaky` swaps the owned-session helper for the acquisition the code used
before the fix - the generator is taken out of the factory and left
suspended at its `yield`, exactly like `async for db in
self.get_db_session():` did. Everything else is the real production code
path: the same controller methods, the same crud calls.

The two runs together are the before/after evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager
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

from bot_module import controller as controller_mod  # noqa: E402
from bot_module.runtime_dependencies import crud  # noqa: E402
from tests.test_controller_db_session_leak import (  # noqa: E402
    _controller,
    _DbFactory,
)


@asynccontextmanager
async def _leaky_owned_session(db_session_factory):
    """The PRE-FIX acquisition: the generator is abandoned at its yield.

    `async for db in self.get_db_session():` walked the factory and left the
    generator suspended inside its `async with AsyncSessionLocal()`. This is
    the same state, reached the same way: take the session out, never close
    the generator.
    """
    gen = db_session_factory()
    db = await gen.__anext__()
    yield db  # the generator is never closed: the connection stays pinned


def _install_leaky(monkey_target) -> None:
    """Make the controller use the pre-fix acquisition."""
    controller_mod.owned_session = _leaky_owned_session


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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--leaky",
        action="store_true",
        help="use the pre-fix acquisition (the generator is abandoned)",
    )
    args = parser.parse_args()

    if args.leaky:
        _install_leaky(controller_mod)

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
        print(
            f"{label:<32} {factory.pool.opened:>7} {pinned:>7}   {verdict}"
        )
    print("-" * 66)
    print(
        f"{'TOTAL PINNED':<32} {'':>7} {failures:>7}   "
        f"{'LEAK' if failures else 'clean'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
