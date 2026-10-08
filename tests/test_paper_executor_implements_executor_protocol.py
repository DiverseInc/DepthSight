# File: tests/test_paper_executor_implements_executor_protocol.py
"""
The paper executor must implement the whole executor protocol the controller calls.

THE DEFECT
----------
`exchanges/base.py` declares `cancel_all_open_orders` on the executor protocol
and `ccxt_executor` implements it. `PaperTradingExecutor` did NOT.

The controller calls it at three places, all of them on the close path:
  - controller.py:10231/10242  `_handle_final_exit` "HARD RESET ... to be 100% safe"
  - controller.py:14747        before a market close
  - controller.py:15216        after close confirmation

In paper mode every one of those raised
    AttributeError: 'PaperTradingExecutor' object has no attribute 'cancel_all_open_orders'

Observed live (2026-10-07 22:27:30):
    ERROR [_HandleFinalExit:BTCUSDT:RETRY_CLOSE_STUCK_CLOSING_x-entry-3036b479472a4b]
    Hard Reset: Error cancelling open orders:
    'PaperTradingExecutor' object has no attribute 'cancel_all_open_orders'

Why that is worse than a crash: all three call sites catch `Exception` and log,
so the close itself still completed. The safety net whose comment says "to be
100% safe" raised, logged an error, and moved on -- leaving the resting SL and
TP orders on the paper book. An orphaned stop-loss can then fire against a
position that no longer exists.

This is the same defect class as the `is_algo_order` `TypeError` (fixed in
2483e88): the paper executor implemented part of the interface, and the
controller assumed all of it.

WHY THIS TEST SHAPE
-------------------
Orders are placed through the REAL `PaperTradingExecutor.place_order`, so the
book being cancelled is the book production builds -- not a hand-rolled dict
that happens to look like it. Nothing from production is copied into the test.

The protocol-parity test at the end is the one that generalises: it compares
`exchanges/base.py` against `PaperTradingExecutor` so the NEXT gap is caught
the day it is written, not the day a user's stop-loss orphans.
"""

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from bot_module.exchanges import base as base_module
from bot_module.paper_executor import PaperTradingExecutor

USER_ID = 10
BTC = "BTCUSDT"
ETH = "ETHUSDT"

REPO_ROOT = Path(__file__).resolve().parents[1]

# Declared in `exchanges/base.py` but genuinely meaningless for a simulated
# book. Each needs a reason, so a NEW absence cannot be quietly allowlisted.
LEGITIMATELY_ABSENT = {
    # Paper orders are all in one RAM dict; there is no separate algo/algo-
    # orders namespace to query, exactly as `cancel_order(is_algo_order=...)`
    # accepts and ignores the flag for interface parity.
    "get_open_algo_orders",
    # Paper has no exchange WebSocket user-data stream; fills are simulated
    # locally in `check_open_orders`.
    "start_user_data_stream",
    "stop_user_data_stream",
}


class _StubDataConsumer:
    """Enough of the data consumer for resting (non-MARKET) paper orders.

    `place_order` only reaches for depth data on the MARKET path; resting
    SL/TP orders are built and stored without it.
    """

    def __init__(self, price: float = 85000.0):
        self.price = price

    async def get_latest_price(self, symbol: str):
        return self.price

    async def get_latest_depth(self, symbol: str):
        return None


def _executor(price: float = 85000.0) -> PaperTradingExecutor:
    return PaperTradingExecutor(
        user_id=USER_ID,
        db_session=None,
        data_consumer=_StubDataConsumer(price),
        redis_client=None,
    )


async def _resting_sl(executor: PaperTradingExecutor, symbol: str, cid: str):
    """A real resting stop-loss on the paper book."""
    return await executor.place_order(
        symbol=symbol,
        side="SELL",
        order_type="STOP_MARKET",
        quantity=0.01,
        stopPrice=80000.0,
        newClientOrderId=cid,
    )


async def _resting_tp(executor: PaperTradingExecutor, symbol: str, cid: str):
    """A real resting take-profit (LIMIT) on the paper book."""
    return await executor.place_order(
        symbol=symbol,
        side="SELL",
        order_type="LIMIT",
        quantity=0.01,
        price=90000.0,
        newClientOrderId=cid,
    )


# --- The defect --------------------------------------------------------------


def test_paper_executor_exposes_cancel_all_open_orders():
    """The attribute the controller resolves must exist, and be a coroutine.

    `await executor.cancel_all_open_orders(...)` on a non-coroutine would
    await a plain dict and raise TypeError instead, so the shape matters.
    """
    fn = getattr(PaperTradingExecutor, "cancel_all_open_orders", None)
    assert fn is not None, (
        "PaperTradingExecutor has no cancel_all_open_orders; the controller's "
        "hard-reset path raises AttributeError on every paper position close"
    )
    assert inspect.iscoroutinefunction(fn)
    assert list(inspect.signature(fn).parameters) == ["self", "symbol"]


@pytest.mark.asyncio
async def test_hard_reset_actually_removes_resting_exit_orders():
    """The seam: what the hard reset promises is that resting exit orders go away.

    Asserts the outcome the controller depends on -- zero resting orders for
    the symbol afterwards -- rather than just that a method returned.
    """
    executor = _executor()
    await _resting_sl(executor, BTC, "x-sl-test")
    await _resting_tp(executor, BTC, "x-ptp-test")
    assert len(executor._open_orders) == 2, "setup: two resting exit orders"

    response = await executor.cancel_all_open_orders(BTC)

    assert isinstance(response, dict)
    assert not response.get("error"), f"hard reset reported an error: {response}"
    remaining = await executor.get_open_orders(BTC)
    assert remaining == [], f"stop-loss/tp survived the hard reset: {remaining}"


@pytest.mark.asyncio
async def test_cancel_all_open_orders_leaves_other_symbols_alone():
    """Symbol-scoped. Cancelling BTC must NOT touch an ETH stop-loss.

    A naive `self._open_orders.clear()` would pass the test above and silently
    strip every other symbol's protection.
    """
    executor = _executor()
    await _resting_sl(executor, BTC, "x-sl-btc")
    await _resting_sl(executor, ETH, "x-sl-eth")

    response = await executor.cancel_all_open_orders(BTC)

    assert response.get("count") == 1
    assert [o["clientOrderId"] for o in await executor.get_open_orders(ETH)] == [
        "x-sl-eth"
    ], "ETH stop-loss was collateral damage"


@pytest.mark.asyncio
async def test_cancel_all_open_orders_returns_the_shape_callers_branch_on():
    """Callers do `isinstance(r, dict) and r.get("error")`.

    Returning a bare list would read as a silent failure: the `else` branch
    logs "cancelled successfully" while nothing was cancelled.
    """
    executor = _executor()
    await _resting_sl(executor, BTC, "x-sl-a")

    response = await executor.cancel_all_open_orders(BTC)

    assert isinstance(response, dict), f"not a dict: {type(response)}"
    assert "error" not in response
    assert response["symbol"] == BTC
    assert response["status"] == "OK"
    assert response["count"] == 1
    assert response["cancelled"] == ["x-sl-a"]


@pytest.mark.asyncio
async def test_cancel_all_open_orders_with_nothing_resting_is_not_an_error():
    """No orders is a successful no-op, not a failure worth retrying."""
    executor = _executor()

    response = await executor.cancel_all_open_orders(BTC)

    assert not response.get("error")
    assert response["count"] == 0
    assert response["cancelled"] == []


@pytest.mark.asyncio
async def test_cancel_all_open_orders_matches_symbol_case_insensitively():
    """The controller passes the symbol as the position spelled it."""
    executor = _executor()
    await _resting_sl(executor, "btcusdt", "x-sl-lower")

    response = await executor.cancel_all_open_orders("BTCUSDT")

    assert response["count"] == 1
    assert await executor.get_open_orders() == []


@pytest.mark.asyncio
async def test_cancelled_orders_are_marked_canceled():
    """Status must flip, as `cancel_order` already does.

    Anything still marked NEW could be re-adopted by the ensure-exit-orders
    sweep as if it were live.
    """
    executor = _executor()
    placed = await _resting_sl(executor, BTC, "x-sl-status")

    await executor.cancel_all_open_orders(BTC)

    assert placed["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_cancel_all_open_orders_is_idempotent():
    """The controller schedules a background retry after a timeout; that retry
    runs against a book the first attempt may already have cleared."""
    executor = _executor()
    await _resting_sl(executor, BTC, "x-sl-retry")

    first = await executor.cancel_all_open_orders(BTC)
    second = await executor.cancel_all_open_orders(BTC)

    assert first["count"] == 1
    assert not second.get("error")
    assert second["count"] == 0


# --- The generalisation: the next gap ----------------------------------------


def test_paper_executor_satisfies_the_declared_executor_protocol():
    """Every method `exchanges/base.py` declares must exist on the paper one.

    This is the test that would have caught 2483e88 and this defect on the day
    they were written. An absence must be added to LEGITIMATELY_ABSENT *with* a
    reason -- never silently.
    """
    source = (REPO_ROOT / "bot_module" / "exchanges" / "base.py").read_text(
        encoding="utf-8"
    )
    declared = set(re.findall(r"async def (\w+)\(", source)) | set(
        re.findall(r"^\s+def (\w+)\(", source, flags=re.MULTILINE)
    )
    assert declared, "failed to parse the executor protocol -- test is useless"

    missing = sorted(
        m
        for m in declared
        if not hasattr(PaperTradingExecutor, m) and m not in LEGITIMATELY_ABSENT
    )

    assert not missing, (
        f"PaperTradingExecutor is missing executor-protocol method(s): {missing}. "
        f"Either implement them, or add them to LEGITIMATELY_ABSENT with a "
        f"reason in tests/test_paper_executor_implements_executor_protocol.py"
    )


def test_legitimately_absent_still_absent():
    """If one of these ever gets implemented, remove it from the allowlist.

    A stale allowlist entry would hide a real regression later.
    """
    now_present = sorted(
        m for m in LEGITIMATELY_ABSENT if hasattr(PaperTradingExecutor, m)
    )
    assert not now_present, (
        f"{now_present} now exist on PaperTradingExecutor; drop them from "
        f"LEGITIMATELY_ABSENT so the parity test keeps its teeth"
    )


def test_base_module_import_is_used():
    """Guard against the `import base as base_module` becoming dead."""
    assert hasattr(base_module, "ExchangeExecutor")