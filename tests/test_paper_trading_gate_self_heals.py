"""A paper account whose balance read recovers must reopen the trading gate.

WHY THIS TEST EXISTS
--------------------
Observed in production on 2026-10-18: every signal from the running paper
strategies was rejected once a minute with

    [ProcessSignal:VisualBuilderStrategy:BTCUSDT:LONG] Signal REJECTED by
    rm.is_symbol_trading_allowed (general block).

`is_symbol_trading_allowed` returns False for exactly three reasons, and the
two that log at WARNING (emergency stop, blacklist) were absent from the log
window. That leaves the global flag `_is_trading_allowed`, whose "trading
globally disabled" branch logs at DEBUG and is therefore invisible on the
dashboard's Critical Events panel.

`initialize_balance()` sets that flag to False when the startup balance fetch
fails (risk_manager.py:571-575). Nothing re-validates it on the paper path:

  * `_check_risk_limits()` is called only from `initialize_balance()`,
    `update_trade_result()` and the LIVE branch of `assess_signal()`.
  * `_check_and_reset_daily_stats()` is called only from
    `update_trade_result()` and the LIVE branch of `assess_signal()`.

So for a paper account that has not closed a trade, neither runs after
startup. A single transient fetch failure at bot start therefore latches
trading off for the entire lifetime of the process -- and the controller's own
gate rejects the signal before `assess_signal()` is ever reached, so the
signal path cannot heal it either.

These tests drive the real RiskManager methods. They assert OUTCOMES, not the
shape of the fix.

2026-10-28: THE PAPER SANDBOX NOW OWNS THE PAPER ACCOUNT'S NUMBERS
--------------------------------------------------------------------
The live and the paper sandboxes are separate (`self.stats` and
`self.paper_stats`), so a keyless PAPER account's balance, day's PnL and
gate now live in the PAPER sandbox (`self.paper_stats` and
`_paper_is_trading_allowed`), not in the shared live ones. The account in
these tests is keyless -- it has no live executor -- so the paper sandbox IS
its sandbox.

The assertions below were updated accordingly. The INTENT of each one --
"the paper account's balance was fetched" and "trading becomes allowed" --
is unchanged; only the sandbox that holds the number, and the sandbox whose
gate is consulted, moved.
"""
import asyncio
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_module.risk_manager import RiskManager, TradeStats  # noqa: E402


class _PaperExecutor:
    """Stands in for PaperTradingExecutor, the balance source for keyless accounts."""

    def __init__(self, balance=None):
        # None -> this executor raises, i.e. the transient startup failure.
        self._balance = balance
        self.calls = 0

    async def get_account_balance(self):
        self.calls += 1
        if self._balance is None:
            raise RuntimeError("paper wallet unavailable")
        return {"USDT": {"free": str(self._balance), "locked": "0.0"}}


def _make_rm(paper_executor, *, balance_threshold=5.0, trading_allowed=False):
    rm = RiskManager.__new__(RiskManager)
    rm.executor = None  # paper-only controller, exactly like bot_runner.py
    rm.paper_executor = paper_executor
    rm.user_id = 1
    rm.stats = type("S", (), {})()
    rm.stats.current_balance = 0.0
    rm.stats.start_of_day_balance = 0.0
    rm.stats.today_pnl = 0.0
    rm.stats.consecutive_losses = 0
    rm.stats.current_trading_day_start_ts = 0.0
    rm.stats.last_known_day_str = None
    rm.min_balance_threshold = balance_threshold
    rm.daily_max_loss_threshold = 0.99
    rm.max_drawdown_threshold = 0.99
    rm.max_consecutive_losses = 5
    # The PAPER sandbox. For an account with no live account behind it, this
    # IS the account's sandbox: its balance, its PnL and its gate.
    rm.paper_stats = TradeStats()
    rm.paper_daily_max_loss_threshold = 0.99
    rm.paper_max_drawdown_threshold = 0.99
    rm.paper_max_consecutive_losses = 5
    rm._paper_is_trading_allowed = trading_allowed
    rm._paper_global_gate_disable_reason = None
    rm._paper_last_disable_reason = None
    rm._paper_balance_known = False
    rm._global_gate_disable_reason = None
    rm.telegram_notifier = None
    rm.loop_from_controller = None
    # db_session=None makes is_symbol_trading_allowed SKIP the blacklist
    # branch entirely, which is what lets these tests prove the rejection
    # came from the global flag and not from a blacklisted symbol.
    rm.db_session = None
    rm._is_trading_allowed = trading_allowed
    rm._emergency_stop_active = False
    rm._emergency_stop_reason = None
    rm._balance_lock = asyncio.Lock()
    # _check_and_reset_daily_stats reads these. _reset_time_utc must match the
    # production value exactly -- risk_manager.py:281 uses a tz-AWARE time,
    # because _get_current_day_start_info compares it against an aware now_utc
    # and a naive datetime.time raises TypeError there.
    rm._reset_time_utc = datetime.time(0, 1, 0, tzinfo=datetime.timezone.utc)
    rm._strategy_symbol_adjustment_enabled = False
    return rm


def test_a_latched_paper_account_cannot_trade():
    """The reported symptom, pinned: the global flag is what rejects the signal.

    Without this, the fix below could 'work' by re-enabling a gate that was
    never the thing rejecting the signal.
    """
    rm = _make_rm(_PaperExecutor(2645.55))
    rm._paper_is_trading_allowed = False

    allowed = asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT", mode="paper"))

    assert allowed is False, (
        "with the global flag off and no db_session (so no blacklist lookup), "
        "is_symbol_trading_allowed must refuse -- this is the exact condition "
        "that produced 'Signal REJECTED by rm.is_symbol_trading_allowed "
        "(general block)'"
    )


def test_reinitializing_with_the_latch_engaged_does_not_open_the_gate():
    """The third unguarded site: `initialize_balance()`'s day-reset branch.

    Found by mutation, not by review. Removing this guard turned NO test red --
    the other two sites were covered and this one silently was not. A guard
    nobody can remove is indistinguishable from a guard that is not there.

    Relevant in production because PR #14 makes the emergency stop survive
    restarts: the latch is now restored, and then `initialize_balance()` runs on
    the very same start.
    """
    rm = _make_rm(_PaperExecutor(2645.55), trading_allowed=False)
    rm.trigger_emergency_stop("operator pulled the plug")

    asyncio.run(rm.initialize_balance())

    assert rm._emergency_stop_active is True
    assert rm._is_trading_allowed is False, (
        "initialize_balance() reset the day and re-enabled the published flag "
        "while the emergency stop was engaged"
    )
    assert rm._paper_is_trading_allowed is False, (
        "initialize_balance() re-enabled the PAPER gate while the emergency "
        "stop was engaged"
    )
    assert asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT")) is False


def test_a_new_utc_day_does_not_clear_the_emergency_stop():
    """A midnight rollover is not an operator action.

    Two reviewers found this after the `_check_risk_limits()` guard was added:
    `_check_and_reset_daily_stats()` set `_is_trading_allowed = True`
    unconditionally on a new UTC day, and `initialize_balance()` does the same
    on its day-reset branch. Both flipped the PUBLISHED flag while a kill switch
    was still engaged, so the dashboard reported "trading allowed" from midnight
    until the operator noticed.
    """
    rm = _make_rm(_PaperExecutor(2645.55), trading_allowed=False)
    rm.trigger_emergency_stop("operator pulled the plug")
    rm.stats.current_balance = 2645.55
    rm.stats.last_known_day_str = "2000-01-01"  # forces a day rollover

    rm._check_and_reset_daily_stats()

    assert rm._emergency_stop_active is True, "the latch itself must survive"
    assert rm._is_trading_allowed is False, (
        "the daily-stat reset re-enabled the published flag while the emergency "
        "stop was still engaged -- the dashboard would report 'trading allowed' "
        "from 00:00 UTC onward"
    )
    assert (
        asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT")) is False
    ), "entries must stay refused across the rollover"


def test_a_new_utc_day_still_reopens_a_merely_limited_account():
    """Positive control for the guard above.

    Without this, a change that simply never re-enabled on a day change would
    pass and would leave a legitimately recovered account blocked forever.
    """
    rm = _make_rm(_PaperExecutor(2645.55), trading_allowed=False)
    rm.stats.current_balance = 2645.55
    rm.stats.last_known_day_str = "2000-01-01"  # forces a day rollover

    rm._check_and_reset_daily_stats()

    assert rm._emergency_stop_active is False
    assert rm._is_trading_allowed is True
    assert asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT")) is True


def test_recovered_paper_balance_reopens_the_trading_gate():
    """THE PROOF.

    The balance fetch has recovered and reports a healthy balance. That read is
    the only trustworthy evidence available, so the gate must be re-evaluated.

    On the unfixed code this fails: `update_balance()` returns True, updates
    stats, and never touches `_is_trading_allowed`.
    """
    paper = _PaperExecutor(2645.55)
    rm = _make_rm(paper, trading_allowed=False)  # latched off, as initialize_balance does

    ok = asyncio.run(rm.update_balance())

    assert ok is True, "the balance fetch itself must succeed for this test to mean anything"
    assert rm.paper_stats.current_balance == pytest.approx(2645.55)
    assert rm._paper_is_trading_allowed is True, (
        "trading stayed disabled after the balance fetch recovered: "
        "update_balance() succeeds but nothing re-validates the global gate on "
        "the paper path, so one transient startup failure latches trading off "
        "for the whole life of the bot process"
    )

    allowed = asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT", mode="paper"))
    assert allowed is True, "the gate must actually reopen, not just the flag flip"


def test_a_genuine_risk_breach_is_not_reopened_by_a_successful_fetch():
    """Negative control. The fix must not become 'always re-enable'.

    An account genuinely below the minimum balance must stay blocked after a
    perfectly successful balance read. This is the test that would catch a
    'fix' that simply forces the flag True.
    """
    paper = _PaperExecutor(3.0)
    rm = _make_rm(paper, balance_threshold=5.0, trading_allowed=True)

    ok = asyncio.run(rm.update_balance())

    assert ok is True
    assert rm._paper_is_trading_allowed is False, (
        "balance $3.00 is below min_balance_threshold $5.00 -- a successful "
        "read must not paper over a real risk breach"
    )
    allowed = asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT", mode="paper"))
    assert allowed is False


def test_a_failed_fetch_does_not_open_the_gate():
    """A read that raised is not evidence. The gate must stay shut."""
    rm = _make_rm(_PaperExecutor(None), trading_allowed=False)

    ok = asyncio.run(rm.update_balance())

    assert ok is False, "a raising executor must report failure"
    assert rm._paper_is_trading_allowed is False, (
        "a failed balance read must never open the trading gate"
    )


def test_emergency_stop_survives_a_successful_balance_refresh():
    """The kill switch is a separate latch and a balance read must not clear it.

    Two independent reviewers found the hole this pins: `_check_risk_limits()`
    sets `_is_trading_allowed = True` on healthy limits, so a periodic refresh
    used to flip that flag back while the latch was still engaged. Entries stayed
    correctly refused -- `is_symbol_trading_allowed` checks
    `_emergency_stop_active` first -- but the controller publishes
    `getattr(self.rm, "_is_trading_allowed", True)` and the API now surfaces it,
    so the dashboard reported "trading allowed" during a kill switch.

    Both halves matter: the refusal AND the published flag.
    """
    rm = _make_rm(_PaperExecutor(2645.55), trading_allowed=False)
    rm.trigger_emergency_stop("operator pulled the plug")

    ok = asyncio.run(rm.update_balance())

    assert ok is True
    assert rm.is_emergency_stop_active() is True
    allowed = asyncio.run(rm.is_symbol_trading_allowed("BTCUSDT", mode="paper"))
    assert allowed is False, (
        "an engaged emergency stop must survive a healthy balance refresh"
    )
    assert rm._paper_is_trading_allowed is False, (
        "the published flag desynced from the latch: _check_paper_risk_limits() "
        "re-enabled _paper_is_trading_allowed while the emergency stop was still "
        "engaged, so GET /api/v1/portfolio would report is_trading_allowed=true "
        "during a kill switch"
    )