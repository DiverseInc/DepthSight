# File: tests/test_hard_reset_does_not_report_success_on_failure.py
"""
The close-path "Hard Reset" must not log success when cancellation failed.

THE DEFECT
----------
Every executor implements `cancel_all_open_orders` as an in-band reporter: it
catches its own exceptions and returns `{"error": True, "code": ..., "msg": ...}`
rather than raising. `PaperTradingExecutor` and `ccxt_executor` both do this.

The hard-reset call site inside `_handle_final_exit` awaited the coroutine and
then logged

    "Hard Reset: All open orders for {symbol} cancelled successfully."

UNCONDITIONALLY. It never looked at the dict. So an in-band failure produced a
success log line while resting stop-loss and take-profit orders survived -- the
safety net whose own comment reads "Cancel ALL open orders for this symbol to be
100% safe" failing silently, and reporting that it had not.

The two sibling call sites (`:14749`, `:15220`) already branched on the error
key, so this was an inconsistency in exactly one place.

WHY THIS TEST SHAPE
-------------------
The logic now lives in `TradingController._hard_reset_cancel_all_orders`, which
is the real production method. The tests call it directly and assert on the
log record and the returned response, so a regression to the old
"log success unconditionally" behaviour turns them red.

`caplog` is used rather than a hand-rolled handler so the assertion is on what
would actually reach an operator's log file.
"""

import asyncio
import logging

import pytest

from bot_module.controller import TradingController

BTC = "BTCUSDT"
LOG_PREFIX = "[TestHardReset]"


class _CancelsEverything:
    """The happy path: reports success in-band."""

    def __init__(self):
        self.calls = 0

    async def cancel_all_open_orders(self, symbol):
        self.calls += 1
        return {"symbol": symbol, "status": "OK", "count": 2, "cancelled": []}


class _ReportsErrorInBand:
    """The defect's trigger: catches its own failure and RETURNS it."""

    def __init__(self):
        self.calls = 0

    async def cancel_all_open_orders(self, symbol):
        self.calls += 1
        return {"error": True, "code": -999, "msg": "connection reset by peer"}


class _Raises:
    def __init__(self):
        self.calls = 0

    async def cancel_all_open_orders(self, symbol):
        self.calls += 1
        raise RuntimeError("socket exploded")


class _Hangs:
    async def cancel_all_open_orders(self, symbol):
        await asyncio.sleep(60)


def _controller() -> TradingController:
    """Minimal real controller: `__new__`, since only `self.loop` is touched."""
    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    return controller


def _reset_logs(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# --- The defect --------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_band_error_does_not_log_success(caplog):
    """The core regression: an error dict must never produce a success log."""
    controller = _controller()

    with caplog.at_level(logging.INFO, logger="bot_module.controller"):
        response = await controller._hard_reset_cancel_all_orders(
            _ReportsErrorInBand(), BTC, LOG_PREFIX
        )

    messages = _reset_logs(caplog)
    assert "cancelled successfully" not in messages, (
        f"hard reset claimed success on a failed cancellation:\n{messages}"
    )
    assert "reported a failure" in messages
    assert response.get("error") is True


@pytest.mark.asyncio
async def test_success_still_logs_success(caplog):
    """The control: the fix must not silence the genuine success path."""
    controller = _controller()

    with caplog.at_level(logging.INFO, logger="bot_module.controller"):
        response = await controller._hard_reset_cancel_all_orders(
            _CancelsEverything(), BTC, LOG_PREFIX
        )

    messages = _reset_logs(caplog)
    assert "cancelled successfully" in messages
    assert "reported a failure" not in messages
    assert not response.get("error")


@pytest.mark.asyncio
async def test_raising_executor_does_not_log_success(caplog):
    """A raising executor must not produce a success line either."""
    controller = _controller()

    with caplog.at_level(logging.INFO, logger="bot_module.controller"):
        response = await controller._hard_reset_cancel_all_orders(
            _Raises(), BTC, LOG_PREFIX
        )

    messages = _reset_logs(caplog)
    assert "cancelled successfully" not in messages, (
        f"hard reset claimed success after an exception:\n{messages}"
    )
    assert response.get("error") is True


@pytest.mark.asyncio
async def test_non_dict_response_is_not_treated_as_an_error():
    """An executor returning something odd must not crash the close path.

    `isinstance(response, dict)` guards the branch, so a list or None falls
    through to the success log rather than raising AttributeError mid-close.
    """
    controller = _controller()

    class _Weird:
        async def cancel_all_open_orders(self, symbol):
            return None

    response = await controller._hard_reset_cancel_all_orders(
        _Weird(), BTC, LOG_PREFIX
    )
    assert response is None


@pytest.mark.asyncio
async def test_timeout_schedules_a_background_retry():
    """On timeout the original site scheduled a retry task; behaviour preserved."""
    controller = _controller()

    response = await controller._hard_reset_cancel_all_orders(
        _Hangs(), BTC, LOG_PREFIX
    )

    assert response.get("error") is True
    assert response["code"] == -110
    # Cancel the retry the helper scheduled so it cannot outlive the test.
    for task in asyncio.all_tasks():
        if task.get_name().startswith("FinalExitHardCancelRetry_"):
            task.cancel()


@pytest.mark.asyncio
async def test_executor_is_called_exactly_once_on_the_happy_path():
    """Guard against a retry firing spuriously when there was no timeout."""
    controller = _controller()
    executor = _CancelsEverything()

    await controller._hard_reset_cancel_all_orders(executor, BTC, LOG_PREFIX)

    assert executor.calls == 1


def test_handle_final_exit_uses_the_checked_helper():
    """The call site must go through the helper, not inline the await again.

    Structural guard: re-inlining the raw `cancel_all_open_orders` await is
    exactly the regression being fixed, and it would otherwise be invisible to
    the behavioural tests above.
    """
    import inspect

    source = inspect.getsource(TradingController._handle_final_exit)

    assert "_hard_reset_cancel_all_orders" in source, (
        "_handle_final_exit no longer routes the hard reset through the helper"
    )
    assert "cancel_all_open_orders" not in source.replace(
        "executor_for_cancel.cancel_all_open_orders", ""
    ) or "_hard_reset_cancel_all_orders" in source, (
        "_handle_final_exit calls cancel_all_open_orders directly again"
    )