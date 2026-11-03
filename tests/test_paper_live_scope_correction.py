"""The PAPER sandbox must not be able to reach into the LIVE sandbox.

THE DEFECT
----------
This branch splits the LIVE and PAPER sandboxes apart so that "a PAPER loss
can never close the LIVE trading gate".  The split was complete for the
risk COUNTERS and the risk LIMITS.  Four paths still crossed the boundary:

1. THE AUTO-BLACKLIST WAS NOT SPLIT.  ``_symbol_stop_timestamps`` was ONE
   dict, so a PAPER stop was recorded in the LIVE stop history and a PAPER
   win ERASED the live stop history.  The stop count then drove
   ``_check_and_apply_auto_blacklist_rules``, which writes the symbol into
   the user's ACCOUNT-LEVEL ``risk_management.blacklist.coins`` -- a record
   with no mode dimension -- and that blacklist is consulted for LIVE
   signals.  So a PAPER stop-loss could blacklist a symbol for real money.

2. THE SCOPE LATCH CLOSED THE LIVE GATE FROM A PAPER FAULT.
   ``_mark_guard_state_scope_unknown("paper")`` set ``_is_trading_allowed =
   False`` -- the LIVE gate -- whatever the scope, so a corrupt ``"paper"``
   payload halted REAL trading for a dual-mode account.

3. THE PAPER GUARD FAILED OPEN.  ``_check_paper_risk_limits`` returned
   EARLY, before the unverified/lost/scope-unknown latches, whenever the
   paper balance was not known yet -- so a lost or unverifiable guard state
   left the PAPER gate open.  The LIVE path checks the latches FIRST.

4. THE PAPER DRAWDOWN WAS NOT THE PEAK-TO-TROUGH MEASURE.  The paper
   ``maxDrawdown`` was computed with the same expression as the paper
   daily loss, so the daily-loss limit was always the tighter of the two and
   the paper drawdown limit could never be the limit that fired -- the very
   defect `main`'s peak-to-trough rewrite fixed for the LIVE gate.

These tests are the RED side of that fix.  Each one FAILS against the code
as it was, and passes only because the boundary is now enforced.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_module import risk_manager as rm_module
from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor


# The clock every day-sensitive path funnels through.
TODAY = "2026-10-27"

LIVE_BALANCE = 10000.0
PAPER_BALANCE = 10000.0

# A 10% drawdown limit and a 5% daily-loss limit, so the two limits
# measure genuinely different quantities.
MAX_DRAWDOWN_PCT = 10.0
DAILY_LOSS_PCT = 5.0


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses."""

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
    return {}


def _settings():
    return {
        "risk_management": {
            "maxDrawdown": MAX_DRAWDOWN_PCT,
            "dailyMaxLossPercent": DAILY_LOSS_PCT,
            "maxConsecutiveLosses": 5,
        }
    }


def _dual_mode_rm(redis_client):
    """A real RiskManager for an account that trades BOTH live and paper."""
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
        user_id=1,
        db_session=None,
        user_settings=_settings(),
        api_key_name="live",
    )
    rm.redis_client = redis_client
    rm.api_key_id = 7
    rm.executor = executor
    rm.paper_executor = paper_executor
    rm.telegram_notifier = None
    rm.loop_from_controller = None
    rm._get_current_day_start_info = lambda: (
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
        TODAY,
    )
    return rm


def _paper_only_rm(redis_client):
    """A real RiskManager for a keyless PAPER account (no live executor)."""
    paper_executor = AsyncMock()
    paper_executor.get_account_balance.return_value = {
        "USDT": {"free": f"{PAPER_BALANCE:.8f}", "locked": "0.00"}
    }
    rm = RiskManager(
        executor=None,
        paper_executor=paper_executor,
        user_id=1,
        db_session=None,
        user_settings=_settings(),
        api_key_name="paper",
    )
    rm.redis_client = redis_client
    rm.api_key_id = None
    rm.paper_executor = paper_executor
    rm.telegram_notifier = None
    rm.loop_from_controller = None
    rm._get_current_day_start_info = lambda: (
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
        TODAY,
    )
    return rm


def _install_fake_config(rm, risk_management, monkeypatch):
    """Give the RiskManager a DB that holds `risk_management`, and nothing else.

    The blacklist is read "on the fly" from the DB, so this is how a test
    puts a blacklist entry in front of the gate.
    """

    class _FakeCrud:
        async def get_config(self, db_session, user_id=None):
            return SimpleNamespace(risk_management=risk_management)

    class _FakeSession:
        def expire_all(self):
            return None

    monkeypatch.setattr(rm_module, "crud", _FakeCrud())
    rm.db_session = _FakeSession()
    rm.user_id = 1
    return rm


# ---------------------------------------------------------------------------
# 1. The auto-blacklist is split per mode
# ---------------------------------------------------------------------------


async def test_a_paper_stop_is_not_recorded_in_the_live_stop_history(redis_store):
    """A PAPER stop-loss must not become part of the LIVE stop history.

    The stop history is what the auto-blacklist rules count.  While it was
    one shared dict, a PAPER stop pushed the LIVE account's stop count up,
    so a live symbol could be blacklisted by simulated losses alone.
    """
    rm = _dual_mode_rm(FakeRedis(redis_store))

    await rm.update_trade_result("ETHUSDT", -50.0, exit_reason="stop_loss", mode="live")
    assert len(rm._symbol_stop_timestamps["ETHUSDT"]) == 1

    await rm.update_trade_result(
        "ETHUSDT", -50.0, exit_reason="stop_loss", mode="paper"
    )

    assert len(rm._symbol_stop_timestamps["ETHUSDT"]) == 1, (
        "a PAPER stop-loss was recorded in the LIVE stop history -- the "
        "auto-blacklist rules count these, so simulated losses could "
        "blacklist a real-money symbol"
    )
    assert len(rm._paper_symbol_stop_timestamps["ETHUSDT"]) == 1, (
        "the PAPER stop history must keep its own count"
    )


async def test_a_paper_win_does_not_erase_the_live_stop_history(redis_store):
    """A PAPER win must not clear the LIVE stop history.

    The other direction of the same defect: a profitable PAPER trade wiped
    the shared stop list, so the LIVE account's auto-blacklist counter was
    reset by simulated money -- which WEAKENS a live risk control.
    """
    rm = _dual_mode_rm(FakeRedis(redis_store))

    await rm.update_trade_result("ETHUSDT", -50.0, exit_reason="stop_loss", mode="live")
    await rm.update_trade_result("ETHUSDT", -30.0, exit_reason="stop_loss", mode="live")
    assert len(rm._symbol_stop_timestamps["ETHUSDT"]) == 2

    await rm.update_trade_result(
        "ETHUSDT", 100.0, exit_reason="take_profit", mode="paper"
    )

    assert len(rm._symbol_stop_timestamps["ETHUSDT"]) == 2, (
        "a PAPER win erased the LIVE stop history -- the live auto-blacklist "
        "counter was reset by simulated money"
    )


async def test_a_paper_blacklist_entry_does_not_block_live_signals(
    redis_store, monkeypatch
):
    """A blacklist entry recorded for the PAPER sandbox must not block LIVE.

    The persisted blacklist is one account-level list, so the entry needs a
    mode dimension.  An entry with no mode at all is a LEGACY entry written
    before the split: it must keep blocking LIVE exactly as it always did.
    """
    rm = _dual_mode_rm(FakeRedis(redis_store))
    _install_fake_config(
        rm,
        {
            "blacklist": {
                "coins": [
                    {
                        "symbol": "ETHUSDT",
                        "until": None,
                        "reason": "Auto-blacklist: 3 consecutive stops",
                        "addedAt": "2026-10-27T00:00:00+00:00",
                        "mode": "paper",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "until": None,
                        "reason": "operator blacklisted",
                        "addedAt": "2026-10-27T00:00:00+00:00",
                    },
                ]
            }
        },
        monkeypatch,
    )
    rm._is_trading_allowed = True
    rm._paper_is_trading_allowed = True

    live_allowed = await rm.is_symbol_trading_allowed("ETHUSDT", mode="live")
    paper_allowed = await rm.is_symbol_trading_allowed("ETHUSDT", mode="paper")
    assert live_allowed is True, (
        "a blacklist entry recorded for the PAPER sandbox blocked a LIVE "
        "signal -- a paper loss closed the live gate"
    )
    assert paper_allowed is False, (
        "the PAPER sandbox's own blacklist entry must still block PAPER"
    )

    # A LEGACY entry (no mode) keeps blocking LIVE, exactly as it always did.
    legacy_live = await rm.is_symbol_trading_allowed("BTCUSDT", mode="live")
    assert legacy_live is False, (
        "a legacy blacklist entry (written before the split) must keep "
        "blocking LIVE -- the live behaviour must not change"
    )


# ---------------------------------------------------------------------------
# 2. A PAPER-scoped fault must not close the LIVE gate
# ---------------------------------------------------------------------------


async def test_a_paper_scoped_fault_does_not_close_the_live_gate(redis_store):
    """A fault in the PAPER payload must not halt REAL trading.

    The unknown-scope latch records WHICH sandbox's day stamp could not be
    read, and the PAPER rollover already releases a PAPER-scoped latch.  But
    the latch itself closed the LIVE gate whatever the scope, so a corrupt
    ``"paper"`` payload stopped a dual-mode account's real money until the
    UTC rollover.
    """
    rm = _dual_mode_rm(FakeRedis(redis_store))
    rm._is_trading_allowed = True
    rm._paper_is_trading_allowed = True

    rm._mark_guard_state_scope_unknown("paper")

    assert rm._is_trading_allowed is True, (
        "a PAPER-scoped fault closed the LIVE gate -- real money was halted "
        "by a corrupt paper payload"
    )
    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate must still fail CLOSED on a PAPER-scoped fault"
    )


async def test_a_live_scoped_fault_still_closes_the_live_gate(redis_store):
    """The LIVE half of the latch is unchanged: a live fault still halts live."""
    rm = _dual_mode_rm(FakeRedis(redis_store))
    rm._is_trading_allowed = True

    rm._mark_guard_state_scope_unknown("live")

    assert rm._is_trading_allowed is False, (
        "a LIVE-scoped fault must still close the LIVE gate"
    )


# ---------------------------------------------------------------------------
# 3. The PAPER guard must fail CLOSED in the same ORDER as the LIVE guard
# ---------------------------------------------------------------------------


async def test_the_paper_guard_fails_closed_on_a_lost_state_with_an_unknown_balance(
    redis_store,
):
    """A lost guard state must close the PAPER gate even before the balance.

    The PAPER path returned early -- before the unverified/lost/scope-unknown
    latches -- whenever the paper balance was not known yet.  So a lost or
    unverifiable guard state left the PAPER gate OPEN.  The LIVE path checks
    the latches first; the PAPER path must do the same.
    """
    rm = _paper_only_rm(FakeRedis(redis_store))
    rm._paper_balance_known = False
    rm._guard_state_lost = True
    rm._paper_is_trading_allowed = True

    rm._check_risk_limits(mode="paper")

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER guard left its gate open on a LOST guard state because the "
        "paper balance was not known yet -- the latches must be checked "
        "FIRST, exactly as the live path checks them"
    )


# ---------------------------------------------------------------------------
# 4. The PAPER drawdown is the PEAK-TO-TROUGH measure
# ---------------------------------------------------------------------------


async def test_the_paper_drawdown_is_measured_from_the_paper_high_water_mark(
    redis_store,
):
    """The paper drawdown must be measured from the paper high-water mark.

    The paper ``maxDrawdown`` used the same expression as the paper daily
    loss, so the daily-loss limit was always the tighter of the two and the
    drawdown limit could never fire.  This is the case that expression
    cannot express: the paper wallet is 12% below its high-water mark but
    made money TODAY, so the old expression read 0%.
    """
    rm = _paper_only_rm(FakeRedis(redis_store))

    # The paper wallet's balance is read: that read is what establishes the
    # PAPER high-water mark (and it is the only thing that may).
    await rm.update_balance(mode="paper")

    rm.paper_stats.current_balance = 8800.0
    rm.paper_stats.start_of_day_balance = 10000.0
    rm.paper_stats.start_of_day_day_str = TODAY
    rm.paper_stats.today_pnl = 100.0
    rm._paper_is_trading_allowed = True

    rm._check_risk_limits(mode="paper")

    assert rm._paper_is_trading_allowed is False, (
        "the paper wallet is below its high-water mark, so the paper "
        "drawdown limit must close the paper gate -- the daily-loss "
        "expression reads 0% on a profitable day"
    )
    reason = rm._paper_global_gate_disable_reason
    assert reason and "drawdown" in reason.lower(), (
        f"the paper gate must name the drawdown as the cause, got {reason!r}"
    )


async def test_the_live_drawdown_measure_is_unchanged(redis_store):
    """The LIVE half of the peak-to-trough measure must not change.

    The paper sandbox now uses the same measure, and the live path is the
    code `main` shipped: the high-water mark is the live account's, it is
    observed from the LIVE balance read only, and the live drawdown is
    measured from it.
    """
    rm = _dual_mode_rm(FakeRedis(redis_store))
    await rm.update_balance(mode="live")
    rm.stats.current_balance = 8800.0
    rm.stats.start_of_day_balance = 10000.0
    rm.stats.start_of_day_day_str = TODAY
    rm.stats.today_pnl = 100.0
    rm._set_balance_pnl_baseline()
    rm._is_trading_allowed = True

    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a fall from the live high-water mark must close the live gate"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and "drawdown" in reason.lower(), (
        f"the live gate must name the drawdown as the cause, got {reason!r}"
    )


async def test_a_paper_balance_read_cannot_move_the_live_high_water_mark(
    redis_store,
):
    """A PAPER balance must never raise the LIVE high-water mark.

    The high-water mark is a RATIO denominator: raising it with simulated
    money would move the LIVE drawdown limit.
    """
    rm = _dual_mode_rm(FakeRedis(redis_store))
    await rm.update_balance(mode="live")
    live_peak = rm._peak_equity
    assert live_peak == pytest.approx(LIVE_BALANCE)

    # The paper wallet is worth far more than the live one.
    rm.paper_executor.get_account_balance.return_value = {
        "USDT": {"free": "50000.00000000", "locked": "0.00"}
    }
    await rm.update_balance(mode="paper")

    assert rm._peak_equity == live_peak, (
        "a PAPER balance raised the LIVE high-water mark -- simulated money "
        "moved the live drawdown limit"
    )
