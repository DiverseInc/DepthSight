"""The PAPER sandbox and the LIVE sandbox must not share a risk gate.

THE DEFECT
----------
A PAPER-trading loss closed the LIVE-trading risk gate.

``controller.py`` closed a position with::

    await self.rm.update_trade_result(symbol, pnl, exit_reason=reason)

with NO mode argument, so ``update_trade_result()`` added the realised PnL to
the SHARED ``self.stats.today_pnl`` and the SHARED
``self.stats.consecutive_losses``. ``_check_risk_limits()`` reads those shared
counters and closes the global ``self._is_trading_allowed`` flag when a limit
is breached, and ``assess_signal()`` consults that flag BEFORE it takes the
paper/live branch.

So a loss on SIMULATED money was added to the LIVE loss streak, counted
against the LIVE daily-loss limit, and closed the LIVE gate: real-money
trading was blocked by a paper loss, for the same user and api key.

Upstream DepthSight had already solved this (``paper_stats``,
``risk_manager.py:294`` / ``:357`` / ``:924`` / ``:1539``, and
``controller.py:11360``-``:11365``); this fork had lost the separation.

WHAT IS PINNED HERE
-------------------
1. A PAPER loss does NOT close the LIVE gate, and does not touch the LIVE
   counters. This is the test that FAILS on the unfixed code.
2. A LIVE loss STILL closes the LIVE gate. The guard is not weakened.
3. A LIVE loss does not close the PAPER gate, and a PAPER loss closes only the
   PAPER gate.
4. The persisted guard state keeps ONE Redis key -- per user + api key, NOT
   per mode -- and carries BOTH sandboxes in the same value, so the state
   that is already persisted is still found and is NOT reset on deploy.

These tests drive the REAL ``RiskManager`` with only the exchange handle, the
Redis client and the clock faked. The end state asserted is the product's,
not a stub's.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor

# The dedicated namespace for the bot's private guard state, written as a
# LITERAL so this file pins the wire contract independently of the
# implementation's plumbing.
GUARD_STATE_NAMESPACE = "depthsight:state:risk"

TODAY = "2026-10-27"

LIVE_BALANCE = 10000.0
PAPER_BALANCE = 5000.0

# Big enough to breach every live limit: a 50% daily loss against a
# $10,000 opening balance, and a loss streak of 1 against a limit of 3.
A_LOSS_THAT_BREACHES_EVERY_LIMIT = -5000.0
MAX_CONSECUTIVE_LOSSES = 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses.

    The store is a dict and is deliberately SHARED between the RiskManagers
    below: handing the same store to a second RiskManager is exactly what
    "the bot restarted" means here.
    """

    def __init__(self, store=None):
        self.store = {} if store is None else store
        self.set_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.set_calls.append((key, value))
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


def _dual_mode_risk_manager(redis_client, day=TODAY, user_id=1, api_key_id=7):
    """A real RiskManager for an account that trades BOTH live and paper.

    The clock is faked by overriding ``_get_current_day_start_info``, which
    every day-sensitive path funnels through.
    """
    executor = AsyncMock(spec=ExchangeExecutor)
    executor.get_account_balance.return_value = {
        "USDT": {"free": f"{LIVE_BALANCE:.8f}", "locked": "0.00"}
    }
    paper_executor = AsyncMock()
    paper_executor.get_account_balance.return_value = {
        "USDT": {"free": f"{PAPER_BALANCE:.8f}", "locked": "0.00"}
    }
    rm = RiskManager(
        executor=executor,
        paper_executor=paper_executor,
        user_id=user_id,
        db_session=None,
        user_settings={
            "risk_management": {"maxConsecutiveLosses": MAX_CONSECUTIVE_LOSSES}
        },
        api_key_name="live",
    )
    rm.redis_client = redis_client
    rm.api_key_id = api_key_id
    rm.executor = executor
    rm.paper_executor = paper_executor
    rm._fake_day = day
    rm._get_current_day_start_info = lambda: (
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
        rm._fake_day,
    )
    return rm


def _guard_key(rm):
    return f"{GUARD_STATE_NAMESPACE}:{rm.user_id}:{rm.api_key_id}"


async def _book_closed_trade(rm, symbol, pnl, mode):
    """Book a CLOSED trade the way the controller does.

    The controller closes a position by passing the position's own mode, so
    that is what this helper does too.

    On the UNFIXED code ``update_trade_result()`` has no ``mode`` argument at
    all and every trade -- paper or live -- lands in the shared live
    counters. The helper falls back to that old call so the test fails on
    the OUTCOME (the live gate closed) rather than on a TypeError about a
    missing keyword argument.
    """
    try:
        await rm.update_trade_result(
            symbol, pnl, exit_reason="STOP_LOSS", mode=mode
        )
    except TypeError as exc:
        if "mode" not in str(exc):
            raise
        await rm.update_trade_result(symbol, pnl, exit_reason="STOP_LOSS")


async def _start_dual_mode_account(redis_store):
    """A started, healthy account that trades both live and paper."""
    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    assert rm._is_trading_allowed is True, (
        "the account must start with the LIVE gate open, or the tests below "
        "would pass for the wrong reason"
    )
    assert rm.stats.current_balance == pytest.approx(LIVE_BALANCE)
    return rm


# ---------------------------------------------------------------------------
# 1. THE CORE DEFECT: a PAPER loss must not close the LIVE gate
# ---------------------------------------------------------------------------


async def test_a_paper_loss_does_not_close_the_live_gate(redis_store):
    """A loss on simulated money must not block real money.

    This is the test that FAILS on the unfixed code: there, the paper loss
    was added to the shared live counters, the live daily-loss limit was
    breached, and ``_is_trading_allowed`` went to False.
    """
    rm = await _start_dual_mode_account(redis_store)

    await _book_closed_trade(
        rm, "BTCUSDT", A_LOSS_THAT_BREACHES_EVERY_LIMIT, mode="paper"
    )

    assert rm._is_trading_allowed is True, (
        "a PAPER loss closed the LIVE gate: real-money trading was blocked "
        "by a simulated loss"
    )
    assert rm.stats.today_pnl == pytest.approx(0.0), (
        "the PAPER loss was added to the LIVE day's PnL"
    )
    assert rm.stats.consecutive_losses == 0, (
        "the PAPER loss was added to the LIVE loss streak"
    )

    # ...and the loss was booked into the PAPER sandbox, where it belongs.
    assert rm.paper_stats.today_pnl == pytest.approx(A_LOSS_THAT_BREACHES_EVERY_LIMIT)
    assert rm.paper_stats.consecutive_losses == 1


async def test_the_live_gate_still_refuses_a_live_symbol_after_a_paper_loss(
    redis_store,
):
    """The gate a LIVE signal meets must still be the LIVE gate.

    A paper loss must not close it, and -- just as importantly -- a paper
    loss must not leave it so open that a live signal would sail through a
    live limit that IS breached. This books a paper loss AND a live loss and
    checks that only the live loss closed the live gate.
    """
    rm = await _start_dual_mode_account(redis_store)

    await _book_closed_trade(rm, "BTCUSDT", -100.0, mode="paper")
    assert rm._is_trading_allowed is True

    await _book_closed_trade(
        rm, "BTCUSDT", A_LOSS_THAT_BREACHES_EVERY_LIMIT, mode="live"
    )
    assert rm._is_trading_allowed is False, (
        "a LIVE loss that breaches the live limits must close the LIVE gate"
    )
    assert rm.stats.today_pnl == pytest.approx(A_LOSS_THAT_BREACHES_EVERY_LIMIT)


# ---------------------------------------------------------------------------
# 2. THE GUARD IS NOT WEAKENED: a LIVE loss must still close the LIVE gate
# ---------------------------------------------------------------------------


async def test_a_live_loss_still_closes_the_live_gate(redis_store):
    """The live path must be exactly as strict as it was.

    This test passes on the unfixed code too -- that is the point. It is the
    counterweight to the test above: separating the sandboxes must not have
    cost the live account any protection.
    """
    rm = await _start_dual_mode_account(redis_store)

    await _book_closed_trade(
        rm, "BTCUSDT", A_LOSS_THAT_BREACHES_EVERY_LIMIT, mode="live"
    )

    assert rm._is_trading_allowed is False, (
        "the live daily-loss limit no longer closes the live gate: the guard "
        "has been weakened"
    )
    assert rm.stats.consecutive_losses == 1
    assert rm.paper_stats.today_pnl == pytest.approx(0.0), (
        "a LIVE loss was booked into the PAPER sandbox"
    )


async def test_the_live_consecutive_loss_still_closes_the_live_gate(redis_store):
    """The streak limit is still enforced, on the live counters only."""
    rm = await _start_dual_mode_account(redis_store)

    for _ in range(MAX_CONSECUTIVE_LOSSES):
        await _book_closed_trade(rm, "BTCUSDT", -1.0, mode="live")

    assert rm.stats.consecutive_losses == MAX_CONSECUTIVE_LOSSES
    assert rm._is_trading_allowed is False, (
        "the live consecutive-loss limit no longer closes the live gate"
    )


async def test_a_live_loss_does_not_close_the_paper_gate(redis_store):
    """The mirror of the core defect: the two gates are independent."""
    rm = await _start_dual_mode_account(redis_store)
    await rm.update_balance(mode="paper")
    assert rm.paper_stats.current_balance == pytest.approx(PAPER_BALANCE)

    await _book_closed_trade(
        rm, "BTCUSDT", A_LOSS_THAT_BREACHES_EVERY_LIMIT, mode="live"
    )

    assert rm._is_trading_allowed is False
    assert rm._paper_is_trading_allowed is True, (
        "a LIVE loss closed the PAPER gate: the live account's losses must "
        "not stop the paper sandbox from trading"
    )


async def test_a_paper_loss_closes_the_paper_gate(redis_store):
    """The paper sandbox is still guarded -- the separation is not a free pass."""
    rm = await _start_dual_mode_account(redis_store)
    await rm.update_balance(mode="paper")
    assert rm.paper_stats.current_balance == pytest.approx(PAPER_BALANCE)

    await _book_closed_trade(
        rm, "BTCUSDT", A_LOSS_THAT_BREACHES_EVERY_LIMIT, mode="paper"
    )

    assert rm._paper_is_trading_allowed is False, (
        "the paper sandbox's own daily-loss limit is no longer enforced"
    )
    assert rm._is_trading_allowed is True, (
        "the paper gate's closure must not have closed the LIVE gate"
    )


# ---------------------------------------------------------------------------
# 3. THE PERSISTED GUARD STATE: one key, both sandboxes, no reset on deploy
# ---------------------------------------------------------------------------


async def test_the_persisted_guard_keeps_one_key_and_carries_both_sandboxes(
    redis_store,
):
    """ONE key, per user + api key, carrying BOTH sandboxes.

    The key is NOT given a mode dimension: the state that is already
    persisted under the old key would then no longer be found, which
    silently RESETS the trading guard for every account on deploy. So the
    live counters keep their existing top-level names and the paper counters
    ride along in the same value.
    """
    rm = await _start_dual_mode_account(redis_store)
    await _book_closed_trade(rm, "BTCUSDT", -10.0, mode="paper")

    key = _guard_key(rm)
    assert key in redis_store, (
        f"the guard state must be persisted under {key!r} -- the SAME key as "
        "before, so the already-persisted state is not lost"
    )
    assert set(redis_store) == {key, f"{key}:seen"}, (
        f"the only keys may be the guard key and the pre-existing 'has "
        f"traded before' marker, got {sorted(redis_store)} -- a mode-scoped "
        "key would leave the already-persisted state behind"
    )

    payload = json.loads(redis_store[key])

    # The LIVE counters keep their existing top-level names, so a payload
    # written by the previous version of this code is still readable.
    assert "consecutive_losses" in payload
    assert "today_pnl" in payload
    assert "start_of_day_balance" in payload
    assert payload["consecutive_losses"] == 0
    assert payload["today_pnl"] == pytest.approx(0.0)

    # ...and the PAPER counters are in the same value.
    assert "paper" in payload
    assert payload["paper"]["consecutive_losses"] == 1
    assert payload["paper"]["today_pnl"] == pytest.approx(-10.0)


async def test_a_payload_written_before_this_change_is_still_restored(
    redis_store,
):
    """NO RESET ON DEPLOY: the already-persisted live guard state is found.

    The payload below is in the OLD shape -- the live counters at the top
    level and no "paper" key at all -- and it carries a loss streak that
    breaches the limit. If the change had given the key a mode dimension, or
    moved the live counters, this payload would not be found and the account
    would start with an EMPTY streak and an OPEN gate.
    """
    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    redis_store[_guard_key(rm)] = json.dumps(
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 7,
            "today_pnl": -500.0,
            "start_of_day_balance": 1000.0,
            "start_of_day_day_str": TODAY,
        }
    )

    restored = await rm.restore_state()

    assert restored is True, "the pre-existing guard state was not found"
    assert rm.stats.consecutive_losses == 7, (
        "the persisted LIVE loss streak was lost: the guard has been RESET "
        "for every account on deploy"
    )
    assert rm.stats.today_pnl == pytest.approx(-500.0)
    assert rm._is_trading_allowed is False, (
        "the persisted LIVE streak breaches the live limits, so the LIVE gate "
        "must still be closed after the restore"
    )
    # The paper sandbox had never been persisted, so it starts empty --
    # which is correct, not a reset.
    assert rm.paper_stats.consecutive_losses == 0
    assert rm.paper_stats.today_pnl == pytest.approx(0.0)
