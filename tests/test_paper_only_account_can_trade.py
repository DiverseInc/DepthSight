"""A keyless paper account must be able to fetch a balance and enable trading.

WHY THIS TEST EXISTS
--------------------
`RiskManager.update_balance()` read only `self.executor`. But
`bot_runner.py:563` deliberately constructs a paper-only RiskManager as:

    RiskManager(executor=None, paper_executor=paper_executor, ...)

For every user with no API key -- i.e. exactly the "demo trade with no API key"
audience -- `self.executor` is None, so the call raised
`AttributeError: 'NoneType' object has no attribute 'get_account_balance'`,
`initialize_balance()` set `_is_trading_allowed = False`, and nothing ever
cleared it. Every signal was then dropped with
`Signal REJECTED by rm.is_symbol_trading_allowed (general block)`.

Observed live: balance pinned at 10,000 with "trade is running" in the UI.

This asserts the OUTCOME (trading becomes allowed), not the shape of the fix,
so re-ordering the fallback inside update_balance cannot make it pass vacuously.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_module.risk_manager import RiskManager  # noqa: E402


class _PaperOnlyExecutor:
    """Stands in for PaperTradingExecutor: the real balance source for keyless accounts."""

    def __init__(self, balance: float = 10000.0):
        self._balance = balance
        self.calls = 0

    async def get_account_balance(self):
        self.calls += 1
        return {"USDT": {"free": str(self._balance), "locked": "0.0"}}


def _make_rm(paper_executor):
    rm = RiskManager.__new__(RiskManager)
    rm.executor = None                      # the whole point: no live executor
    rm.paper_executor = paper_executor
    rm.user_id = 10
    rm.stats = type("S", (), {})()
    rm.stats.current_balance = 0.0
    rm.stats.start_of_day_balance = 0.0
    rm.stats.today_pnl = 0.0
    rm.stats.consecutive_losses = 0
    rm.stats.current_trading_day_start_ts = 0.0
    rm.stats.last_known_day_str = None
    rm.min_balance_threshold = 0.0
    rm.daily_max_loss_threshold = 0.99
    rm.max_drawdown_threshold = 0.99
    rm.max_consecutive_losses = 5
    rm.telegram_notifier = None
    rm.loop_from_controller = None
    rm._is_trading_allowed = False
    rm._emergency_stop_active = False
    rm._emergency_stop_reason = None

    import asyncio as _a
    rm._balance_lock = _a.Lock()
    return rm


def test_paper_only_account_fetches_balance_without_live_executor():
    """update_balance must succeed with executor=None by using paper_executor."""
    paper = _PaperOnlyExecutor(10000.0)
    rm = _make_rm(paper)
    assert rm.executor is None

    ok = asyncio.run(rm.update_balance())

    assert paper.calls >= 1, "paper executor was never consulted"
    assert ok is True, "update_balance must succeed for a keyless paper account"
    assert rm.stats.current_balance == pytest.approx(10000.0)


def test_paper_only_account_enables_trading_after_initialize():
    """The user-visible outcome: initialize_balance must leave trading ALLOWED.

    This is the assertion that would have caught the original bug. It fails on
    the unfixed code with _is_trading_allowed == False.
    """
    paper = _PaperOnlyExecutor(10000.0)
    rm = _make_rm(paper)

    # Neutralise _check_risk_limits so this test isolates the balance fetch.
    rm._check_risk_limits = lambda: None
    rm._get_current_day_start_info = lambda: (0.0, "2026-10-05")

    asyncio.run(rm.initialize_balance())

    assert rm.stats.current_balance == pytest.approx(10000.0)
    assert rm._is_trading_allowed is True, (
        "trading stayed disabled: initialize_balance could not read the paper "
        "balance, so the RiskManager blocks every signal with 'general block'"
    )


def test_live_executor_is_still_preferred_when_present():
    """A real key must keep using the LIVE executor, not silently fall back to paper."""

    class _Live(_PaperOnlyExecutor):
        pass

    live = _Live(500.0)
    paper = _PaperOnlyExecutor(10000.0)
    rm = _make_rm(paper)
    rm.executor = live

    ok = asyncio.run(rm.update_balance())

    assert ok is True
    assert rm.stats.current_balance == pytest.approx(500.0)
    assert live.calls == 1
    assert paper.calls == 0, "paper executor must not be consulted when a live executor exists"


def test_no_executor_at_all_fails_closed_not_open():
    """No executor AND no paper executor must NOT enable trading."""
    rm = _make_rm(None)
    ok = asyncio.run(rm.update_balance())
    assert ok is False
    assert rm._is_trading_allowed is False