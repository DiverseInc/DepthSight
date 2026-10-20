# File: tests/test_paper_pnl_reaches_risk_manager.py
"""
A paper close must reach the RiskManager with the REAL net PnL.

THE DEFECT
----------
`PaperTradingExecutor` computed the real realized PnL of every fill and
booked it to the paper WALLET (`total_balance_change = realized_pnl -
actual_commission_paid`) and to the DB trade row (`"pnl": realized_pnl`).
The RiskManager never saw any of it.

`_notify_controller_about_fill` -- the only place the executor writes back
onto the controller's `LivePosition` -- updated `remaining_quantity` and the
status, and never the PnL. The controller's finalization then PRESERVED
`position.pnl` (still the 0.0 default) and handed THAT number to the risk
manager:

    controller.py:  await self.rm.update_trade_result(symbol, position.pnl, ...)

So in paper mode the risk manager was told every trade closed at exactly
zero profit. Three consequences, all measured rather than theoretical:

  1. `RiskManager.stats.today_pnl` stayed 0.0 forever, so the daily-loss
     guard and the drawdown guard -- both of which read
     `abs(today_pnl / start_of_day_balance)` -- evaluated to 0% and were
     STRUCTURALLY DISARMED in paper mode.
  2. `consecutive_losses` counted CLOSED POSITIONS instead of losing
     trades (`if pnl <= 0:` is true for 0.0) and could never be reset by a
     win, because `pnl > 0` was unreachable in paper mode.
  3. The UTC-day rollover that clears those counters was reachable only
     from `update_trade_result()` and from the LIVE branch of
     `assess_signal()`. The PAPER branch returns before that call, so the
     rollover ran only when a position closed -- but the daily-loss guard
     closes the gate precisely when no position can be opened, so no close
     could fire. The paper bot was deadlocked until the process restarted.

WHAT THIS TEST PROVES
---------------------
It drives the REAL executor (`check_open_orders` -> `place_order`), the REAL
controller finalization (`_handle_final_exit`) and the REAL `RiskManager`
(`update_trade_result`). The PnL arithmetic is NOT re-implemented here: the
oracle is the MONEY. The paper wallet is credited `realized_pnl - commission`
on every fill, and `api/routes/portfolio.py` reports `sum(pnl - commission)`
to the user, so the number the risk manager is given must be exactly the
number the wallet moved.

Red-then-green: `git stash` the two source files, re-run this file, and the
PnL tests and the daily-loss test fail -- the risk manager is told 0.0 --
while the live-path test stays green (it is the control).
"""

import asyncio
import logging
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from bot_module.controller import (
    ActivePositionMap,
    LivePosition,
    SignalDirection,
    TradingController,
)
from bot_module.paper_executor import PaperTradingExecutor
from bot_module.risk_manager import RiskManager
from bot_module.runtime_dependencies import crud

USER_ID = 7
SYMBOL = "BTCUSDT"
MARKET_TYPE = "futures_usdtm"

ENTRY_PRICE = 100.0
QTY = 1.0


def _utc_day_str(offset_days: int = 0) -> str:
    """The `YYYY-MM-DD` UTC day key the risk manager rolls over on."""
    day = datetime.now(timezone.utc).date() + timedelta(days=offset_days)
    return day.strftime("%Y-%m-%d")


# --- Test doubles: inputs only, never behaviour under test --------------------


class _FakeDataConsumer:
    """The only price source the paper executor reads."""

    def __init__(self, price: float):
        self.price = price

    async def get_latest_price(self, symbol):
        return self.price

    async def get_latest_depth(self, symbol):
        return None


class _FakeDB:
    """The paper executor's whole DB surface on the close path."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, obj, *args, **kwargs):
        return None


class _FakeTradeLogger:
    """Stand-in for the controller's trade_logger (it only has to not raise)."""

    def log_order_placed(self, *args, **kwargs):
        pass

    def log_event(self, *args, **kwargs):
        pass


class _FakeRedis:
    async def get(self, key):
        return None

    async def set(self, key, value):
        return True

    async def zadd(self, *args, **kwargs):
        return 1

    async def zremrangebyscore(self, *args, **kwargs):
        return 0


class _FakeLiveExecutor:
    """Stand-in for a real exchange connection, for the live-path control."""

    market_type = MARKET_TYPE

    def __init__(self):
        self.cancelled = []

    async def cancel_order(self, **kwargs):
        self.cancelled.append(kwargs)
        return {"error": False}

    async def cancel_all_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return {"error": False}

    async def get_account_balance(self):
        return {"USDT": {"free": "1000.0", "locked": "0.0"}}


# --- The MONEY: the oracle ----------------------------------------------------


@pytest.fixture
def money(monkeypatch):
    """Records what the paper WALLET is credited/debited, and the DB rows.

    The wallet delta IS the net PnL of a close: the executor credits
    `realized_pnl - actual_commission_paid`. It is the same number
    `api/routes/portfolio.py` shows the user as `sum(pnl - commission)`, so
    it is the number the risk manager has to agree with.
    """
    record = {"wallet": [], "trades": [], "wallet_balance": 1000.0}

    async def _fake_update_paper_wallet_balance(
        db, user_id, asset, amount, *args, **kwargs
    ):
        record["wallet"].append({"asset": asset, "amount": float(amount)})
        return None

    async def _fake_create_trade(db, *args, **kwargs):
        record["trades"].append(kwargs.get("trade_data") or {})
        return types.SimpleNamespace(id=4242)

    async def _fake_get_paper_wallet(db, user_id=None, *args, **kwargs):
        return [
            types.SimpleNamespace(asset="USDT", balance=float(record["wallet_balance"]))
        ]

    monkeypatch.setattr(
        crud, "update_paper_wallet_balance", _fake_update_paper_wallet_balance
    )
    monkeypatch.setattr(crud, "create_trade", _fake_create_trade)
    monkeypatch.setattr(crud, "get_paper_wallet", _fake_get_paper_wallet)

    # `place_order` imports the analytics task lazily. A celery app is noise
    # in a unit test, not behaviour under test.
    fake_tasks = types.ModuleType("tasks")
    fake_tasks.process_live_trade_analytics_task = MagicMock()
    monkeypatch.setitem(sys.modules, "tasks", fake_tasks)

    return record


# --- The world ----------------------------------------------------------------


def _build_controller(price: float):
    """A real RiskManager and a real PaperTradingExecutor, and a controller
    carrying only the attribute surface the code under test touches.

    Built with `__new__` because `TradingController.__init__` opens exchange
    connections; every method used below is the real, unbound production one.

    Returns (controller, paper_executor, risk_manager).
    """
    data_consumer = _FakeDataConsumer(price)
    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=data_consumer,
        redis_client=None,
    )
    live_executor = _FakeLiveExecutor()

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = None
    controller.api_key_name = "paper"
    controller.executors = {"live": live_executor, "paper": paper_executor}
    controller.market_executors = {}
    controller.consumer = data_consumer
    controller.redis_client = _FakeRedis()
    controller.redis_key_runtime_state = f"runtime_state:{USER_ID}"
    controller.db_session = None
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller._symbol_locks = {}
    controller._market_info_cache = {}
    controller._market_info_lock = asyncio.Lock()
    controller._last_position_close_time_per_symbol = {}
    controller.telegram_notifier = None
    controller.user_telegram_chat_id = None
    controller.trade_logger = _FakeTradeLogger()
    controller.realtime_ml_logger = None
    controller._closing_managed_symbols = set()
    # `_handle_final_exit` schedules a state publish; wire the two attributes
    # it reads so the fire-and-forget task does not log an unretrieved
    # exception and mask the result of the test.
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {}

    controller.rm = RiskManager(
        executor=live_executor,
        paper_executor=paper_executor,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    paper_executor.controller = controller

    return controller, paper_executor, controller.rm


def _seed_long(paper_executor, controller, *, entry_price, qty, mode="paper"):
    """Opens a paper LONG: the executor's book AND the controller's mirror."""
    now = datetime.now(timezone.utc)
    paper_executor._positions[SYMBOL] = {
        "quantity": qty,
        "avg_entry_price": entry_price,
        "entry_timestamp": now,
        "leverage": 1.0,
        # No funding boundary is crossed, so the only wallet movement in the
        # test is the close itself.
        "last_funding_timestamp": now,
        "strategy_config_id": None,
        "entry_client_order_id": "x-entry-test-00000001",
    }
    position = LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        entry_price=entry_price,
        initial_quantity=qty,
        remaining_quantity=qty,
        entry_time=time.time() - 3600,
        strategy="TestStrategy",
        status="OPEN",
        mode=mode,
        market_type=MARKET_TYPE,
        user_id=None,
        config_id=6999,
        entry_client_order_id="x-entry-test-00000001",
        entry_order_id="2200861597",
        entry_order_status="FILLED",
    )
    controller._active_position_set(position)
    return position


def _rest_exit_order(paper_executor, *, qty, price, order_type):
    """Rests the exit order the way the controller's paper book does."""
    client_order_id = f"x-{order_type.lower()}-test-00000001"
    paper_executor._open_orders[client_order_id] = {
        "symbol": SYMBOL,
        "orderId": 2200861599,
        "clientOrderId": client_order_id,
        "price": None if order_type == "STOP_MARKET" else str(price),
        "stopPrice": str(price) if order_type == "STOP_MARKET" else None,
        "origQty": str(qty),
        "executedQty": "0.0",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": order_type,
        "side": "SELL",
        "time": int(time.time() * 1000),
        "strategy_config_id": None,
        "signal_details": None,
    }
    return client_order_id


async def _finalize(controller, reason: str):
    """Runs the REAL controller finalization of a paper close.

    This is the call the stuck-CLOSING recovery makes
    (`close_position` -> `_handle_final_exit(exit_price=0.0)`): a
    finalization-only close, which preserves the PnL the fill already
    booked instead of recomputing it.
    """
    await controller._handle_final_exit(
        SYMBOL,
        reason,
        exit_price=0.0,
        commission=0.0,
        commission_asset=None,
        order_id=None,
        client_order_id=None,
        realized_pnl_from_exchange=0.0,
        exchange_pnl_available=False,
        market_type=MARKET_TYPE,
    )


async def _run_paper_close(
    *,
    mark_price,
    exit_price,
    order_type,
    entry_price=ENTRY_PRICE,
    qty=QTY,
    start_of_day_balance=None,
):
    """A full paper close: resting exit order -> fill.

    Returns (controller, risk_manager, position).
    """
    controller, paper_executor, rm = _build_controller(mark_price)
    position = _seed_long(
        paper_executor, controller, entry_price=entry_price, qty=qty
    )

    # Pin the day so the UTC rollover cannot fire and be mistaken for the
    # effect under test.
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = (
        1000.0 if start_of_day_balance is None else start_of_day_balance
    )

    _rest_exit_order(
        paper_executor, qty=qty, price=exit_price, order_type=order_type
    )

    # The real tick: the resting order is consumed and filled by the real
    # executor, which books the money and reports the fill to the controller.
    await paper_executor.check_open_orders()

    return controller, rm, position


# --- 1. A paper LOSS reaches the risk manager --------------------------------


@pytest.mark.asyncio
async def test_paper_loss_reaches_the_risk_manager_as_a_negative_net_pnl(money):
    """A losing paper close must tell the risk manager it lost money.

    Before the fix the risk manager was told 0.0: `today_pnl` stayed at 0.0
    and `consecutive_losses` counted every closed position instead of every
    losing trade.
    """
    controller, rm, position = await _run_paper_close(
        mark_price=90.0,
        exit_price=90.0,
        order_type="STOP_MARKET",
    )

    # The MONEY moved by the net PnL of the close. This is the oracle.
    assert len(money["wallet"]) == 1, (
        f"expected exactly one wallet movement for one close, got "
        f"{money['wallet']}"
    )
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta < 0, f"a stop-out must debit the wallet, got {wallet_delta}"

    await _finalize(controller, "STOP_LOSS")

    assert rm.stats.today_pnl == pytest.approx(wallet_delta, abs=1e-9), (
        f"the risk manager was told {rm.stats.today_pnl!r} but the wallet moved "
        f"{wallet_delta!r}; the guard and the API must read the same number"
    )
    assert rm.stats.today_pnl < 0, (
        "today_pnl must go negative on a paper loss, otherwise the daily-loss "
        "guard can never fire"
    )
    assert rm.stats.consecutive_losses == 1, (
        f"one losing close must count as one loss, got "
        f"{rm.stats.consecutive_losses}"
    )


# --- 2. A paper PROFIT reaches the risk manager and resets the streak -------


@pytest.mark.asyncio
async def test_paper_profit_reaches_the_risk_manager_and_resets_the_streak(money):
    """A winning paper close must RESET the consecutive-loss counter.

    This is impossible before the fix: the risk manager was told 0.0, so
    `pnl > 0` was unreachable and the streak could only ever count up.
    """
    controller, rm, position = await _run_paper_close(
        mark_price=110.0,
        exit_price=110.0,
        order_type="LIMIT",
    )

    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta > 0, f"a take-profit must credit the wallet, got {wallet_delta}"

    # A streak that is already running must be cleared by the win.
    rm.stats.consecutive_losses = 3

    await _finalize(controller, "TAKE_PROFIT")

    assert rm.stats.today_pnl == pytest.approx(wallet_delta, abs=1e-9), (
        f"the risk manager was told {rm.stats.today_pnl!r} but the wallet moved "
        f"{wallet_delta!r}"
    )
    assert rm.stats.today_pnl > 0, (
        "today_pnl must go positive on a paper profit"
    )
    assert rm.stats.consecutive_losses == 0, (
        f"a winning close must reset the loss streak, got "
        f"{rm.stats.consecutive_losses}"
    )


# --- 3. Commission decides whether a close is a loss ------------------------


@pytest.mark.asyncio
async def test_commission_can_turn_a_gross_profit_into_a_loss(money):
    """The risk manager must see the NET result, not the gross one.

    The API reports `sum(pnl - commission)` over today's closed paper
    trades, so a close that is profitable before commission and a loss after
    it is a loss. A gross-PnL guard would call this a win.
    """
    # 10 units at 100.10: the fill is at 100.10 * (1 - 0.0005) = 100.04995,
    # so the price move is worth +0.4995 gross, but the commission on the
    # fill is 100.04995 * 10 * 0.0006 = 0.6002997, so the account is down
    # 0.1007997.
    controller, rm, position = await _run_paper_close(
        mark_price=100.10,
        exit_price=100.10,
        order_type="LIMIT",
        qty=10.0,
    )

    gross = money["trades"][0]["pnl"]
    commission = money["trades"][0]["commission"]
    assert gross > 0, f"the fill must be gross profitable, got {gross}"
    assert commission > gross, (
        f"the commission must outweigh the gross profit, got "
        f"commission={commission} gross={gross}"
    )

    expected_net = gross - commission
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta == pytest.approx(expected_net, abs=1e-9)
    assert expected_net == pytest.approx(-0.1007997, abs=1e-9), (
        f"the net result changed: {expected_net}"
    )

    await _finalize(controller, "TAKE_PROFIT")

    assert rm.stats.today_pnl == pytest.approx(-0.1007997, abs=1e-9), (
        f"the risk manager must see the NET result, got {rm.stats.today_pnl!r}"
    )
    assert rm.stats.today_pnl < 0, "a net-negative close is a LOSS"
    assert rm.stats.consecutive_losses == 1, (
        f"a net-negative close must count as a loss, got "
        f"{rm.stats.consecutive_losses}"
    )


# --- 4. The daily-loss guard can fire in paper mode --------------------------


@pytest.mark.asyncio
async def test_daily_loss_guard_fires_when_the_days_real_loss_exceeds_the_limit(
    money,
):
    """The daily-loss guard must be able to close the gate in paper mode.

    Before the fix `today_pnl` was stuck at 0.0, so
    `abs(today_pnl / start_of_day_balance)` was 0% and the guard was
    structurally disarmed: it could never fire, in either direction.
    """
    controller, rm, position = await _run_paper_close(
        mark_price=40.0,
        exit_price=40.0,
        order_type="STOP_MARKET",
        start_of_day_balance=1000.0,
    )

    wallet_delta = money["wallet"][0]["amount"]
    loss_pct = abs(wallet_delta / rm.stats.start_of_day_balance)
    assert loss_pct > rm.daily_max_loss_threshold, (
        f"the test must exceed the daily limit: {loss_pct * 100:.2f}% > "
        f"{rm.daily_max_loss_threshold * 100:.2f}%"
    )

    await _finalize(controller, "STOP_LOSS")

    assert rm._is_trading_allowed is False, (
        "the daily-loss guard must close the gate when the day's real loss "
        "exceeds the limit"
    )
    assert "Daily loss" in (rm._global_gate_disable_reason or ""), (
        f"the gate must name the daily-loss breach, got "
        f"{rm._global_gate_disable_reason!r}"
    )


@pytest.mark.asyncio
async def test_daily_loss_guard_does_not_fire_within_the_limit(money):
    """The other direction: a guard that always fires is as broken as one
    that never fires.

    A paper loss INSIDE the daily limit must leave the gate open.
    """
    controller, rm, position = await _run_paper_close(
        mark_price=95.0,
        exit_price=95.0,
        order_type="STOP_MARKET",
        start_of_day_balance=1000.0,
    )

    wallet_delta = money["wallet"][0]["amount"]
    loss_pct = abs(wallet_delta / rm.stats.start_of_day_balance)
    assert loss_pct < rm.daily_max_loss_threshold, (
        f"the test must stay inside the daily limit: {loss_pct * 100:.2f}%"
    )

    await _finalize(controller, "STOP_LOSS")

    assert rm.stats.today_pnl == pytest.approx(wallet_delta, abs=1e-9)
    assert rm._is_trading_allowed is True, (
        f"a loss inside the daily limit must not close the gate, got "
        f"{rm._global_gate_disable_reason!r}"
    )


# --- 5. FIX 2: the UTC-day rollover must not need a trade to close ----------


@pytest.mark.asyncio
async def test_balance_update_rolls_the_day_over_without_a_trade_closing(money):
    """The gate must be able to heal on a new UTC day with NO trade closing.

    The rollover used to run only from `update_trade_result()` and from the
    LIVE branch of `assess_signal()`. The PAPER branch returns before that
    call, so on the paper path the rollover ran only when a position closed
    -- but the daily-loss guard closes the gate precisely when no position
    can be opened, so no close could fire and the bot was deadlocked until
    the process restarted.
    """
    _, _, rm = _build_controller(price=100.0)
    # A keyless paper account has no live executor, so the PAPER WALLET is
    # the balance source -- exactly as bot_runner.py builds it.
    rm.executor = None

    # The blocked state: over the consecutive-loss limit, gate closed.
    rm.stats.consecutive_losses = rm.max_consecutive_losses
    rm.stats.today_pnl = -100.0
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0
    rm._check_risk_limits()
    assert rm._is_trading_allowed is False, "precondition: the gate is closed"

    # A balance update on the SAME UTC day must NOT clear the counters.
    rm.stats.last_known_day_str = _utc_day_str()
    assert await rm.update_balance() is True
    assert rm.stats.consecutive_losses == rm.max_consecutive_losses, (
        "a balance update inside the same UTC day must not clear the "
        "consecutive-loss counter"
    )
    assert rm.stats.today_pnl == -100.0, (
        "a balance update inside the same UTC day must not clear today's PnL"
    )
    assert rm._is_trading_allowed is False, (
        "a balance update inside the same UTC day must not re-open the gate"
    )

    # A balance update on a NEW UTC day MUST clear them -- the documented
    # daily rollover, with no trade closing.
    rm.stats.last_known_day_str = _utc_day_str(offset_days=-1)
    assert await rm.update_balance() is True
    assert rm.stats.consecutive_losses == 0, (
        "a new UTC day must reset the consecutive-loss counter even when no "
        "trade closed"
    )
    assert rm.stats.today_pnl == 0.0, (
        "a new UTC day must reset today's PnL even when no trade closed"
    )
    assert rm._is_trading_allowed is True, (
        "a new UTC day must re-open the gate when no risk limit is in force"
    )


@pytest.mark.asyncio
async def test_balance_update_does_not_reopen_the_gate_under_an_emergency_stop(
    money,
):
    """The daily rollover must not clear an operator's kill switch.

    `_check_and_reset_daily_stats()` re-opens the gate on a new day only
    when the emergency-stop latch is not engaged, and `_check_risk_limits()`
    does the same. Running the rollover from the balance update must not
    change that: the latch survives, the gate stays shut, and the stop
    reason is still reported.
    """
    _, _, rm = _build_controller(price=100.0)
    # A keyless paper account has no live executor, so the PAPER WALLET is
    # the balance source -- exactly as bot_runner.py builds it.
    rm.executor = None

    rm.stats.consecutive_losses = rm.max_consecutive_losses
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0
    rm.trigger_emergency_stop("operator pulled the kill switch")

    rm.stats.last_known_day_str = _utc_day_str(offset_days=-1)
    assert await rm.update_balance() is True

    assert rm.stats.consecutive_losses == 0, (
        "the day-scoped counters are reset on a new day even under an "
        "emergency stop"
    )
    assert rm.is_emergency_stop_active() is True, "the latch itself must survive"
    assert rm._is_trading_allowed is False, (
        "a new UTC day must NOT re-open the gate while the emergency-stop "
        "latch is engaged"
    )
    assert "EMERGENCY STOP" in (rm._trading_disabled_reason or ""), (
        f"the stop reason must still be reported, got "
        f"{rm._trading_disabled_reason!r}"
    )


@pytest.mark.asyncio
async def test_new_day_balance_update_still_leaves_a_breached_gate_closed(money):
    """ORDER: the rollover must run BEFORE the limit re-check.

    The rollover clears the day-scoped counters and re-opens the gate. The
    limit check that follows re-detects a breach that is NOT day-scoped -- a
    balance below the minimum -- and closes the gate again. With the two
    calls in the other order the gate would be left OPEN past a real
    breach.
    """
    money["wallet_balance"] = 3.0
    _, _, rm = _build_controller(price=100.0)
    # A keyless paper account has no live executor, so the PAPER WALLET is
    # the balance source -- exactly as bot_runner.py builds it.
    rm.executor = None

    # Already below the minimum balance, and blocked because of it.
    rm.stats.current_balance = 3.0
    rm.stats.start_of_day_balance = 3.0
    rm._check_risk_limits()
    assert rm._is_trading_allowed is False, "precondition: the gate is closed"

    # A NEW UTC day: the rollover clears the day-scoped counters and opens
    # the gate -- but the minimum-balance breach is still in force.
    rm.stats.last_known_day_str = _utc_day_str(offset_days=-1)
    assert await rm.update_balance() is True

    assert rm._is_trading_allowed is False, (
        "a balance still below the minimum must keep the gate closed after a "
        "new-day rollover; the limit check has to run AFTER the rollover"
    )
    assert "Balance" in (rm._global_gate_disable_reason or ""), (
        f"the gate must name the balance breach, got "
        f"{rm._global_gate_disable_reason!r}"
    )


# --- 7. A LOST PnL booking must be LOUD --------------------------------------


@pytest.mark.asyncio
async def test_a_lost_pnl_booking_is_logged_loudly(money, caplog):
    """The wallet moves BEFORE the risk manager is told. If the booking
    fails, the money is gone from the account but the risk manager still
    believes the trade never closed -- so the loss must be logged at ERROR
    with that fact, not swallowed.

    This is the fail-OPEN direction: the PnL is LOST, not double-counted.
    The control flow is unchanged (the fill still succeeds), but the loss
    must be visible.
    """
    controller, paper_executor, rm = _build_controller(price=90.0)
    _seed_long(paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY)

    # Make the fill NOTIFICATION blow up, and only that. The first
    # `_active_position_get` call is the executor reading the entry CID
    # before the fill; the second is the notification booking the PnL.
    # Failing the second one is the case under test: the wallet has already
    # been credited by `place_order`, and the risk manager is never told.
    real_get = controller._active_position_get
    calls = {"n": 0}

    def _explode_on_the_notify(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get(*args, **kwargs)
        raise RuntimeError("simulated notify failure")

    controller._active_position_get = _explode_on_the_notify

    _rest_exit_order(
        paper_executor, qty=QTY, price=90.0, order_type="STOP_MARKET"
    )

    with caplog.at_level(logging.ERROR, logger="bot_module.paper_executor"):
        await paper_executor.check_open_orders()

    assert calls["n"] >= 2, "the notification must have been attempted"

    # The MONEY moved: place_order credited the wallet before the notify.
    assert len(money["wallet"]) == 1, (
        f"expected exactly one wallet movement, got {money['wallet']}"
    )
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta < 0, f"a stop-out must debit the wallet, got {wallet_delta}"

    # The risk manager was NOT told -- and that must be LOUD.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, (
        "a lost PnL booking must be logged at ERROR; the wallet moved but the "
        "risk manager was not told, so today's PnL and the consecutive-loss "
        "streak are now wrong"
    )
    text = "\n".join(r.getMessage() for r in errors)
    assert "risk manager was NOT told" in text, (
        f"the log must say the risk manager was not told; got: {text}"
    )
    assert "already credited" in text, (
        f"the log must say the wallet was already credited; got: {text}"
    )


@pytest.mark.asyncio
async def test_a_lost_liquidation_pnl_booking_is_logged_loudly(money, caplog):
    """The LIQUIDATION path is the one that logged at DEBUG.

    A lost PnL booking there was invisible: the wallet was credited and
    nothing said so. It must be an ERROR, like the TP/SL path, and it must
    say that the risk manager was not told.
    """
    controller, paper_executor, rm = _build_controller(price=90.0)
    _seed_long(paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY)
    # leverage 10 puts the liquidation price at 100 * 0.9 / 0.995 = 90.45,
    # so a mark of 90.0 liquidates the position.
    paper_executor._positions[SYMBOL]["leverage"] = 10.0

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated notify failure")

    controller._active_position_get = _explode

    with caplog.at_level(logging.ERROR, logger="bot_module.paper_executor"):
        await paper_executor.check_open_orders()

    # The MONEY moved: the liquidation credited the wallet before the notify.
    assert len(money["wallet"]) == 1, (
        f"expected exactly one wallet movement, got {money['wallet']}"
    )
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta < 0, f"a liquidation must debit the wallet, got {wallet_delta}"

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, (
        "a lost PnL booking on the liquidation path must be logged at ERROR, "
        "not DEBUG: the wallet moved but the risk manager was not told"
    )
    text = "\n".join(r.getMessage() for r in errors)
    assert "risk manager was NOT told" in text, (
        f"the log must say the risk manager was not told; got: {text}"
    )


@pytest.mark.asyncio
async def test_the_liquidation_path_books_the_NET_pnl_not_the_gross_one(money):
    """MUTATION KILLER: the liquidation path must book the NET result.

    The liquidation path computes its OWN commission
    (`exit_price * qty * BACKTEST_COMMISSION_PCT`), credits the wallet
    `realized_pnl - commission`, and must hand the risk manager THE SAME
    number.

    Before this was pinned, replacing the net with the gross SURVIVED this
    whole file: the liquidation test only asserted that a FAILED booking was
    logged loudly -- it never checked what number a SUCCESSFUL one booked.
    The gross and the net differ by the commission, and the risk manager
    must be given the NET -- the same number the wallet moved. In this
    fixture the gross is already negative, so what is pinned here is the
    exact NUMBER, not the sign: the sibling test
    `test_commission_can_turn_a_gross_profit_into_a_loss` is the one that
    pins the sign, on the MARKET branch.
    """
    controller, paper_executor, rm = _build_controller(price=90.0)
    position = _seed_long(paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY)
    # leverage 10 puts the liquidation price at 100 * 0.9 / 0.995 = 90.45,
    # so a mark of 90.0 liquidates the position.
    paper_executor._positions[SYMBOL]["leverage"] = 10.0

    await paper_executor.check_open_orders()

    assert len(money["trades"]) == 1, (
        f"expected exactly one closed trade, got {money['trades']}"
    )
    gross = float(money["trades"][0]["pnl"])
    commission = float(money["trades"][0]["commission"])
    assert commission > 0, (
        f"the liquidation must pay a commission, got {commission}"
    )
    expected_net = gross - commission

    # The MONEY is the oracle: the wallet moved by the NET result.
    assert len(money["wallet"]) == 1, (
        f"expected exactly one wallet movement, got {money['wallet']}"
    )
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta == pytest.approx(expected_net, abs=1e-9), (
        f"the wallet moved {wallet_delta} but gross-commission is {expected_net}"
    )

    # The risk manager is told the position's booked PnL, so THAT is what has
    # to be the net result too. This is the object the controller's
    # finalization reads and hands to `update_trade_result`.
    booked = float(position.pnl or 0.0)
    assert booked == pytest.approx(expected_net, abs=1e-9), (
        f"the liquidation booked {booked} on the position; the risk manager "
        f"must be given the NET result {expected_net} (gross {gross} minus "
        f"commission {commission}), never the gross one"
    )


# --- 6. The live path is unchanged -------------------------------------------


@pytest.mark.asyncio
async def test_live_close_still_delivers_the_exchange_pnl_unchanged(money):
    """CONTROL: the live path must be untouched by this change.

    The live path never goes through the paper executor's fill notification;
    it takes the realized PnL the exchange reported. This test is green
    BEFORE the fix as well as after it -- it exists to catch a regression,
    not to demonstrate the defect.
    """
    controller, paper_executor, rm = _build_controller(price=100.0)

    position = _seed_long(
        paper_executor, controller, entry_price=100.0, qty=1.0, mode="live"
    )
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0

    EXCHANGE_PNL = -12.3456
    await controller._handle_final_exit(
        SYMBOL,
        "STOP_LOSS",
        exit_price=87.6544,
        commission=0.06,
        commission_asset="USDT",
        order_id=2200861599,
        client_order_id="x-sl-live-00000001",
        realized_pnl_from_exchange=EXCHANGE_PNL,
        exchange_pnl_available=True,
        market_type=MARKET_TYPE,
    )

    assert rm.stats.today_pnl == pytest.approx(EXCHANGE_PNL, abs=1e-9), (
        f"the live path must still deliver the exchange's realized PnL, got "
        f"{rm.stats.today_pnl!r}"
    )
    assert rm.stats.consecutive_losses == 1

    # The paper executor's fill notification must not have run: the live
    # path's PnL comes from the exchange, not from the paper book.
    assert position.pnl == pytest.approx(EXCHANGE_PNL, abs=1e-9)
