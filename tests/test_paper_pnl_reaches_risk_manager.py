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
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_module.controller import (
    ActivePositionMap,
    LivePosition,
    PartialTpOrderInfo,
    SignalDirection,
    TradingController,
)
from bot_module.datatypes import OrderMode, StrategySignal
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

    # THE PAPER WALLET'S BALANCE IS READ FIRST, exactly as the product does:
    # the PAPER gate is judged against the PAPER wallet's balance, and
    # `_check_paper_risk_limits()` refuses to judge an UNKNOWN number -- so
    # without this read the paper limits are never judged at all and the
    # daily-loss guard can never fire, in either direction.
    #
    # It must come BEFORE the pinning below: `update_balance()` only records
    # the balance as KNOWN when it CHANGED, so pinning `current_balance`
    # first would make the read a no-op and the gate would stay unjudged.
    assert await rm.update_balance(mode="paper") is True

    # Pin the day so the UTC rollover cannot fire and be mistaken for the
    # effect under test. BOTH sandboxes are pinned: these are PAPER trades,
    # so they are booked into the PAPER sandbox, and an unpinned PAPER day
    # would roll over and zero the paper day's PnL mid-test.
    for _stats in (rm.stats, rm.paper_stats):
        _stats.last_known_day_str = _utc_day_str()
        _stats.current_balance = 1000.0
        _stats.start_of_day_balance = (
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

    assert rm.paper_stats.today_pnl == pytest.approx(wallet_delta, abs=1e-9), (
        f"the risk manager was told {rm.paper_stats.today_pnl!r} but the wallet moved "
        f"{wallet_delta!r}; the guard and the API must read the same number"
    )
    assert rm.paper_stats.today_pnl < 0, (
        "today_pnl must go negative on a paper loss, otherwise the daily-loss "
        "guard can never fire"
    )
    assert rm.paper_stats.consecutive_losses == 1, (
        f"one losing close must count as one loss, got "
        f"{rm.paper_stats.consecutive_losses}"
    )
    # The LIVE sandbox must not have heard about it. This was simulated
    # money: booking it against the live day's PnL is the defect this
    # branch exists to fix (a paper loss closing the live gate).
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER loss was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
    )
    assert rm.stats.consecutive_losses == 0, (
        f"the PAPER loss was counted in the LIVE loss streak "
        f"({rm.stats.consecutive_losses})"
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

    # A streak that is already running must be cleared by the win. It is the
    # PAPER streak: this is a paper account, so its loss streak lives in the
    # paper sandbox.
    rm.paper_stats.consecutive_losses = 3

    await _finalize(controller, "TAKE_PROFIT")

    assert rm.paper_stats.today_pnl == pytest.approx(wallet_delta, abs=1e-9), (
        f"the risk manager was told {rm.paper_stats.today_pnl!r} but the wallet moved "
        f"{wallet_delta!r}"
    )
    assert rm.paper_stats.today_pnl > 0, (
        "today_pnl must go positive on a paper profit"
    )
    assert rm.paper_stats.consecutive_losses == 0, (
        f"a winning close must reset the loss streak, got "
        f"{rm.paper_stats.consecutive_losses}"
    )
    # The LIVE sandbox must not have heard about it.
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER profit was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
    )
    assert rm.stats.consecutive_losses == 0, (
        f"the PAPER win was booked against the LIVE loss streak "
        f"({rm.stats.consecutive_losses})"
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

    assert rm.paper_stats.today_pnl == pytest.approx(-0.1007997, abs=1e-9), (
        f"the risk manager must see the NET result, got "
        f"{rm.paper_stats.today_pnl!r}"
    )
    assert rm.paper_stats.today_pnl < 0, "a net-negative close is a LOSS"
    assert rm.paper_stats.consecutive_losses == 1, (
        f"a net-negative close must count as a loss, got "
        f"{rm.paper_stats.consecutive_losses}"
    )
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER close was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
    )
    assert rm.stats.consecutive_losses == 0, (
        f"the PAPER close was counted in the LIVE loss streak "
        f"({rm.stats.consecutive_losses})"
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
    loss_pct = abs(wallet_delta / rm.paper_stats.start_of_day_balance)
    assert loss_pct > rm.paper_daily_max_loss_threshold, (
        f"the test must exceed the daily limit: {loss_pct * 100:.2f}% > "
        f"{rm.paper_daily_max_loss_threshold * 100:.2f}%"
    )

    await _finalize(controller, "STOP_LOSS")

    assert rm._paper_is_trading_allowed is False, (
        "the daily-loss guard must close the PAPER gate when the day's real "
        "loss exceeds the limit"
    )
    assert "Daily loss" in (rm._paper_global_gate_disable_reason or ""), (
        f"the gate must name the daily-loss breach, got "
        f"{rm._paper_global_gate_disable_reason!r}"
    )
    # The LIVE gate must still be open: this loss was simulated money, and a
    # paper loss closing the live gate is the defect this branch fixes.
    assert rm._is_trading_allowed is True, (
        f"a PAPER loss closed the LIVE gate, reason "
        f"{rm._global_gate_disable_reason!r}"
    )
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER loss was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
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
    loss_pct = abs(wallet_delta / rm.paper_stats.start_of_day_balance)
    assert loss_pct < rm.paper_daily_max_loss_threshold, (
        f"the test must stay inside the daily limit: {loss_pct * 100:.2f}%"
    )

    await _finalize(controller, "STOP_LOSS")

    assert rm.paper_stats.today_pnl == pytest.approx(wallet_delta, abs=1e-9)
    assert rm._paper_is_trading_allowed is True, (
        f"a loss inside the daily limit must not close the PAPER gate, got "
        f"{rm._paper_global_gate_disable_reason!r}"
    )
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER close was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
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

    # The PAPER wallet's balance must be KNOWN before the paper limits can be
    # judged (`_check_paper_risk_limits` refuses to judge an unknown number),
    # so read it first -- exactly what the real start-up does.
    assert await rm.update_balance() is True

    # The blocked state: over the consecutive-loss limit, gate closed. These
    # are the PAPER sandbox's counters: they are the ones this account's gate
    # reads.
    rm.paper_stats.consecutive_losses = rm.paper_max_consecutive_losses
    rm.paper_stats.today_pnl = -100.0
    rm.paper_stats.current_balance = 1000.0
    rm.paper_stats.start_of_day_balance = 1000.0
    rm._check_risk_limits(mode="paper")
    assert rm._paper_is_trading_allowed is False, (
        "precondition: the gate is closed"
    )

    # A balance update on the SAME UTC day must NOT clear the counters.
    rm.paper_stats.last_known_day_str = _utc_day_str()
    assert await rm.update_balance() is True
    assert rm.paper_stats.consecutive_losses == rm.paper_max_consecutive_losses, (
        "a balance update inside the same UTC day must not clear the "
        "consecutive-loss counter"
    )
    assert rm.paper_stats.today_pnl == -100.0, (
        "a balance update inside the same UTC day must not clear today's PnL"
    )
    assert rm._paper_is_trading_allowed is False, (
        "a balance update inside the same UTC day must not re-open the gate"
    )

    # A balance update on a NEW UTC day MUST clear them -- the documented
    # daily rollover, with no trade closing.
    rm.paper_stats.last_known_day_str = _utc_day_str(offset_days=-1)
    assert await rm.update_balance() is True
    assert rm.paper_stats.consecutive_losses == 0, (
        "a new UTC day must reset the consecutive-loss counter even when no "
        "trade closed"
    )
    assert rm.paper_stats.today_pnl == 0.0, (
        "a new UTC day must reset today's PnL even when no trade closed"
    )
    assert rm._paper_is_trading_allowed is True, (
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

    # The PAPER wallet's balance must be KNOWN before the paper limits can be
    # judged.
    assert await rm.update_balance() is True

    rm.paper_stats.consecutive_losses = rm.paper_max_consecutive_losses
    rm.paper_stats.current_balance = 1000.0
    rm.paper_stats.start_of_day_balance = 1000.0
    rm._check_risk_limits(mode="paper")
    assert rm._paper_is_trading_allowed is False, (
        "precondition: the paper gate is closed"
    )

    rm.trigger_emergency_stop("operator pulled the kill switch")

    rm.paper_stats.last_known_day_str = _utc_day_str(offset_days=-1)
    assert await rm.update_balance() is True

    assert rm.paper_stats.consecutive_losses == 0, (
        "the day-scoped counters are reset on a new day even under an "
        "emergency stop"
    )
    assert rm.is_emergency_stop_active() is True, "the latch itself must survive"
    assert rm._paper_is_trading_allowed is False, (
        "a new UTC day must NOT re-open the PAPER gate while the "
        "emergency-stop latch is engaged"
    )
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


# --- 8. The controller's OWN finalization must book the NET too ------------
#
# `_handle_final_exit` prefers the exchange's realized PnL (the 'rp' field).
# When the exchange reported none -- SPOT has no 'rp' at all, and a futures
# ORDER_TRADE_UPDATE can omit it -- it falls back to recomputing the PnL from
# the entry and exit prices. That recomputation is GROSS: it is
# `(exit - entry) * qty`, with no commission term anywhere in it.
#
# The commission WAS computed, in the same function, and recorded on
# `position.total_commission` -- but it was never subtracted from the value
# that was booked. So the risk manager was handed the GROSS result of a close
# whose NET result was a loss: `today_pnl` counted a profit the account never
# made, and `consecutive_losses` never incremented for it.
#
# These tests pin the exact NUMBER, and the oracle does NOT depend on the
# production helper: the gross and the commission are the facts the fixture
# seeds, and the booked number must be their difference.


@pytest.mark.asyncio
async def test_the_final_exit_fallback_books_the_NET_pnl_not_the_gross_one(money):
    """MUTATION KILLER: the CALCULATED-PnL fallback must book the NET.

    This is the hole that `opencode-reviewer` and `dsh-reviewer` both found
    in this branch: the fallback assigned the GROSS to `position.pnl`, and
    `position.pnl` is what is handed to `rm.update_trade_result`.

    Removing the commission subtraction from that assignment must turn this
    test RED. It is the only thing that pins it.
    """
    controller, paper_executor, rm = _build_controller(price=100.40)
    position = _seed_long(
        paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY, mode="live"
    )
    # The commission the exchange charged on the opening fill.
    position.entry_commission = 0.06
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0

    EXIT_PRICE = 100.40
    EXIT_COMMISSION = 0.06024  # 100.40 * 1.0 * 0.0006

    # No 'rp' from the exchange: the calculated fallback is taken.
    await controller._handle_final_exit(
        SYMBOL,
        "STOP_LOSS",
        exit_price=EXIT_PRICE,
        commission=EXIT_COMMISSION,
        commission_asset="USDT",
        order_id=2200861599,
        client_order_id="x-sl-live-00000001",
        realized_pnl_from_exchange=0.0,
        exchange_pnl_available=False,
        market_type=MARKET_TYPE,
    )

    gross = (EXIT_PRICE - ENTRY_PRICE) * QTY
    total_commission = 0.06 + EXIT_COMMISSION
    expected_net = gross - total_commission

    # The commission of the legs the fallback recomputed must be recorded,
    # and it must be the commission those legs actually cost.
    assert position.total_commission == pytest.approx(total_commission, abs=1e-9), (
        f"the position recorded a commission of {position.total_commission!r}; "
        f"the entry ({0.06}) plus this close ({EXIT_COMMISSION}) is "
        f"{total_commission!r}"
    )

    # The risk manager must be given the NET result, never the gross one.
    assert rm.stats.today_pnl == pytest.approx(expected_net, abs=1e-9), (
        f"the risk manager was told {rm.stats.today_pnl!r}; the gross result "
        f"is {gross!r} and the NET result is {expected_net!r} (gross minus "
        f"the commission {total_commission!r})"
    )
    assert rm.stats.today_pnl != pytest.approx(gross, abs=1e-9), (
        f"the risk manager was told the GROSS result {gross!r}; a gross "
        f"number is a number the account never earned"
    )


@pytest.mark.asyncio
async def test_commission_can_turn_the_fallbacks_gross_profit_into_a_loss(money):
    """The sign is the point: a gross profit that is a net LOSS is a loss.

    The daily-loss guard reads `today_pnl`, and the consecutive-loss guard
    reads `pnl <= 0`. Handed the gross, this close reads as a WIN and the
    guards never fire for it. Handed the net, it reads as the loss it is.
    """
    controller, paper_executor, rm = _build_controller(price=100.05)
    position = _seed_long(
        paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY, mode="live"
    )
    position.entry_commission = 0.06
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0

    EXIT_PRICE = 100.05
    EXIT_COMMISSION = 0.06003  # 100.05 * 1.0 * 0.0006

    await controller._handle_final_exit(
        SYMBOL,
        "TAKE_PROFIT",
        exit_price=EXIT_PRICE,
        commission=EXIT_COMMISSION,
        commission_asset="USDT",
        order_id=2200861599,
        client_order_id="x-tp-live-00000001",
        realized_pnl_from_exchange=0.0,
        exchange_pnl_available=False,
        market_type=MARKET_TYPE,
    )

    gross = (EXIT_PRICE - ENTRY_PRICE) * QTY
    expected_net = gross - (0.06 + EXIT_COMMISSION)
    assert gross > 0, f"the close must be gross profitable, got {gross}"
    assert expected_net < 0, (
        f"the close must be a net loss for this test to mean anything, got "
        f"{expected_net}"
    )

    assert rm.stats.today_pnl == pytest.approx(expected_net, abs=1e-9), (
        f"the risk manager was told {rm.stats.today_pnl!r}; the NET result is "
        f"{expected_net!r} and the gross one is {gross!r}"
    )
    assert rm.stats.today_pnl < 0, (
        "a close that is a loss after commission must be booked as a loss, "
        "otherwise the daily-loss guard cannot see it"
    )
    assert rm.stats.consecutive_losses == 1, (
        f"a net-losing close must count as one loss, got "
        f"{rm.stats.consecutive_losses}"
    )


@pytest.mark.asyncio
async def test_the_final_exit_does_not_charge_the_triggering_tps_commission_twice(
    money,
):
    """The commission total must count each leg ONCE.

    When a partial TP closes the position, `_handle_partial_fill` stores that
    fill's commission on the partial TP *and* passes it as the final event's
    commission. Summing the partial TPs and then adding the event charged the
    SAME commission twice, so the total was inflated by one exit leg.

    That was harmless while the total was only RECORDED. It stopped being
    harmless when the total became the thing subtracted from the PnL the
    risk manager is given.
    """
    controller, paper_executor, rm = _build_controller(price=105.0)
    position = _seed_long(
        paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY, mode="live"
    )
    position.entry_commission = 0.06
    # The partial TP that closed the position: FILLED, with its commission.
    position.remaining_quantity = 0.0
    position.partial_tp_orders = [
        PartialTpOrderInfo(
            target_price=105.0,
            orig_fraction=1.0,
            quantity=QTY,
            order_id=2200861700,
            client_order_id="x-ptp-test-00000001",
            status="FILLED",
            fill_price=105.0,
            commission=0.03,
        )
    ]
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0

    await controller._handle_final_exit(
        SYMBOL,
        "ALL_TP_CLOSED_BY_TP_1",
        exit_price=105.0,
        commission=0.03,
        commission_asset="USDT",
        order_id=2200861700,
        client_order_id="x-ptp-test-00000001",
        realized_pnl_from_exchange=0.0,
        exchange_pnl_available=False,
        market_type=MARKET_TYPE,
    )

    gross = (105.0 - ENTRY_PRICE) * QTY
    # The entry commission plus the ONE exit leg this position had.
    total_commission = 0.06 + 0.03
    expected_net = gross - total_commission

    assert position.total_commission == pytest.approx(total_commission, abs=1e-9), (
        f"the position recorded a commission of {position.total_commission!r}; "
        f"the entry ({0.06}) plus the single exit leg ({0.03}) is "
        f"{total_commission!r} -- the triggering partial TP's commission must "
        f"not be charged twice"
    )
    assert rm.stats.today_pnl == pytest.approx(expected_net, abs=1e-9), (
        f"the risk manager was told {rm.stats.today_pnl!r}; the NET result is "
        f"{expected_net!r} and the gross one is {gross!r}"
    )


# --- 9. The Trade row the API reads must carry the GROSS ---------------------
#
# `api/routes/portfolio.py` reports `sum(Trade.pnl - Trade.commission)` to the
# user -- in the PAPER branch (:237) and in the LIVE branch (:407). That is the
# ONLY thing the API computes out of the Trade row, so the row's `pnl` has to
# be the GROSS result and the row's `commission` the commission actually paid.
#
# The controller's own row is the one writer that did not follow that
# convention: it stored the very same `position.pnl` the risk manager is
# given. While that value was the GROSS (before c43ae18) the row was right by
# accident. The moment it became the NET, the API subtracted the commission a
# SECOND time and under-reported the PnL of every LIVE fallback close by
# exactly the total commission.
#
# None of the tests above can kill that: they inspect `position.pnl` and the
# risk manager's `today_pnl`, never the row the API reads. This one does.

_EXIT_ROW_REASON = "STOP_LOSS"


@pytest.mark.asyncio
async def test_the_trade_row_the_api_reads_carries_the_GROSS_pnl(money):
    """MUTATION KILLER: the row must hold the GROSS, beside the commission.

    `controller.get_db_session` is stubbed here. The shared `_build_controller`
    helper never sets it, so the controller's own Trade row raises an
    AttributeError that is swallowed at controller.py:11291 -- the row is
    never visible, and the double subtraction can never be observed. Without
    this stub this test could not exist.
    """
    controller, paper_executor, rm = _build_controller(price=100.40)
    position = _seed_long(
        paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY, mode="live"
    )
    # The commission the exchange charged on the opening fill.
    position.entry_commission = 0.06
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0

    EXIT_PRICE = 100.40
    EXIT_COMMISSION = 0.06024  # 100.40 * 1.0 * 0.0006

    async def _fake_get_db_session():
        yield _FakeDB()

    controller.get_db_session = _fake_get_db_session

    class _FakeTradeLoggerWithDiary(_FakeTradeLogger):
        """The real TradeLogger has `log_closed_trade_to_diary`; the shared
        fake does not. The controller's DB row write reuses `r_multiplier_val`,
        which is computed inside that diary block, so without this method the
        row write raises UnboundLocalError and the row is never written.
        """

        def log_closed_trade_to_diary(self, *args, **kwargs):
            pass

    controller.trade_logger = _FakeTradeLoggerWithDiary()

    # No 'rp' from the exchange: the calculated fallback is taken.
    await controller._handle_final_exit(
        SYMBOL,
        _EXIT_ROW_REASON,
        exit_price=EXIT_PRICE,
        commission=EXIT_COMMISSION,
        commission_asset="USDT",
        order_id=2200861599,
        client_order_id="x-sl-live-00000001",
        realized_pnl_from_exchange=0.0,
        exchange_pnl_available=False,
        market_type=MARKET_TYPE,
    )

    gross = (EXIT_PRICE - ENTRY_PRICE) * QTY
    total_commission = 0.06 + EXIT_COMMISSION
    expected_net = gross - total_commission

    # The controller's own row is the row the API reads.
    rows = [r for r in money["trades"] if r.get("exit_reason") == _EXIT_ROW_REASON]
    assert len(rows) == 1, (
        f"expected exactly one controller Trade row, got {money['trades']!r}"
    )
    row = rows[0]

    # The row stores the GROSS result, because the API subtracts the
    # commission itself. A NET value here would be charged the commission
    # twice: the reported PnL would be `gross - 2 * commission`.
    assert row["pnl"] == pytest.approx(gross, abs=1e-9), (
        f"the Trade row holds a pnl of {row['pnl']!r}; it must hold the GROSS "
        f"result {gross!r} and never the NET one {expected_net!r} -- the API "
        f"computes `sum(pnl - commission)`, so a NET value in the row "
        f"subtracts the commission a second time and under-reports the PnL "
        f"by exactly the commission"
    )

    # The commission lives in its own column and is not touched.
    assert row["commission"] == pytest.approx(total_commission, abs=1e-9), (
        f"the Trade row holds a commission of {row['commission']!r}; the entry "
        f"({0.06}) plus this close ({EXIT_COMMISSION}) is {total_commission!r}"
    )

    # And what the API reports is the NET result -- the same number the risk
    # manager was given. The two conventions are split, not confused.
    assert row["pnl"] - row["commission"] == pytest.approx(
        expected_net, abs=1e-9
    ), (
        f"the API would report {row['pnl'] - row['commission']!r}; the NET "
        f"result is {expected_net!r} (gross {gross!r} minus the commission "
        f"{total_commission!r})"
    )
    assert rm.stats.today_pnl == pytest.approx(expected_net, abs=1e-9), (
        f"the risk manager was told {rm.stats.today_pnl!r}; the NET result is "
        f"{expected_net!r} -- the row holds the GROSS {gross!r} and the risk "
        f"manager holds the NET, and both are correct"
    )


# --- 10. The ROUTINE controller close must reach the wallet and the RM ------
#
# THE DEFECT THIS SECTION PINS
#
# `PaperTradingExecutor` never declared `supports_positions`, so the
# controller classified the paper book as a SPOT book
# (controller.py:15381-15383) and entered the SPOT-SYNC block
# (controller.py:15560-15643) on every LONG close. That block reads the
# BASE-asset balance of the wallet -- a `futures_usdtm` paper book holds a
# single USDT row and has no base asset at all -- so `free_base_qty` is 0.0
# and `total_qty` is 0.0, and the block set `remaining_quantity = 0.0`,
# `is_confirmed_closed = True` and BROKE, about 175 lines BEFORE
# `executor.place_order` (controller.py:15818).
#
# Three consequences, all measured below:
#
#   1. the wallet was NEVER credited (paper_executor.py:260 never ran);
#   2. no PAPER trade row was written (paper_executor.py:385-386, :401 never
#      ran);
#   3. `remaining_quantity` had already been zeroed before `_handle_final_exit`
#      read it, so the risk manager was told `-entry_commission` -- a WINNING
#      close was booked as a small LOSS, `consecutive_losses` went up and the
#      daily-loss guard under-counted.
#
# The SL/TP resting-order fill path (`paper_executor.check_open_orders`) was
# NEVER affected -- which is exactly why the tests above never caught it.
#
# THE FIX
#
# The paper executor declares `supports_positions` / `supports_shorting` and
# sets `self.supports_positions = True`, so the controller treats the paper
# book as the FUTURES book it is: it syncs the real quantity, closes through
# `place_order`, and the wallet moves.

_CLOSURE_REASON = "PM_ACTION_CLOSE"


def _seed_short(paper_executor, controller, *, entry_price, qty, mode="paper"):
    """Opens a paper SHORT: the executor's book AND the controller's mirror."""
    now = datetime.now(timezone.utc)
    paper_executor._positions[SYMBOL] = {
        "quantity": -qty,
        "avg_entry_price": entry_price,
        "entry_timestamp": now,
        "leverage": 1.0,
        # No funding boundary is crossed, so the only wallet movement in the
        # test is the close itself.
        "last_funding_timestamp": now,
        "strategy_config_id": None,
        "entry_client_order_id": "x-entry-short-test-00000001",
    }
    position = LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.SHORT,
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
        entry_client_order_id="x-entry-short-test-00000001",
        entry_order_id="2200861598",
        entry_order_status="FILLED",
    )
    controller._active_position_set(position)
    return position


def _seed_paper_book_only(paper_executor, *, entry_price, qty):
    """A position the PAPER BOOK holds and the CONTROLLER does not know about.

    This is the orphan case: the book and the controller disagree. The
    controller's own "an active position already exists" check
    (controller.py:8410-8415) cannot see it -- only the executor's book can.
    """
    now = datetime.now(timezone.utc)
    paper_executor._positions[SYMBOL] = {
        "quantity": qty,
        "avg_entry_price": entry_price,
        "entry_timestamp": now,
        "leverage": 1.0,
        "last_funding_timestamp": now,
        "strategy_config_id": None,
        "entry_client_order_id": "x-entry-orphan-test-00000001",
    }


@pytest.mark.asyncio
async def test_a_paper_long_closed_by_the_controller_credits_the_wallet_and_tells_the_risk_manager(
    money,
):
    """MUTATION KILLER: the ROUTINE close must move the wallet AND the RM.

    This is the path the strategy exit (`PM_ACTION_CLOSE`), the regime-change
    cut, the manual API close and every emergency close take. Before the fix
    it never reached `executor.place_order`: the wallet was not credited, no
    PAPER trade row was written, and the risk manager was told
    `-entry_commission` (a LOSS) for a close that had made money.

    The two numbers are compared with a 2% tolerance, and the tolerance is
    documented rather than hidden. The wallet books the FILL (the simulator's
    0.05% slippage included) while the controller's fallback recomputes from
    the TICKER, and the fallback is told the ENTRY commission but not the
    EXIT leg's. The residual is therefore
    `slippage + (exit commission - entry commission)` -- about 0.6% of the
    result here. What the fix changes is the SIGN: before it the guard said
    "-0.06" while the wallet said "+9.88".
    """
    controller, paper_executor, rm = _build_controller(price=110.0)
    position = _seed_long(
        paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY
    )
    # The commission the opening fill actually paid. The controller knows it,
    # so `_handle_final_exit` subtracts it from the gross result.
    position.entry_commission = 0.06
    # The PAPER wallet's balance is read first: the PAPER gate is judged
    # against it, and an UNKNOWN number is not judged at all.
    assert await rm.update_balance(mode="paper") is True
    # Pin BOTH sandboxes for the day: this close is a PAPER trade, so it is
    # booked into the PAPER sandbox, and an unpinned PAPER day would roll
    # over and zero the paper day's PnL mid-test.
    for _stats in (rm.stats, rm.paper_stats):
        _stats.last_known_day_str = _utc_day_str()
        _stats.current_balance = 1000.0
        _stats.start_of_day_balance = 1000.0

    await controller.close_position(
        SYMBOL, reason=_CLOSURE_REASON, market_type=MARKET_TYPE
    )

    # 1. THE MONEY moved: the close reached `executor.place_order`. The paper
    #    book is a FUTURES book -- there is no base-asset balance in it, and
    #    the controller must not go looking for one.
    assert len(money["wallet"]) == 1, (
        f"the close never reached the executor: the wallet was NOT credited "
        f"({money['wallet']!r})"
    )
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta > 0, (
        f"a winning close must credit the wallet, got {wallet_delta!r}"
    )

    # 2. The API's number: the PAPER row the executor wrote for this fill.
    #    It stores the GROSS result beside the commission, so the API's
    #    `sum(pnl - commission)` is exactly what the wallet moved.
    assert len(money["trades"]) == 1, (
        f"expected exactly one PAPER trade row for one close, got "
        f"{money['trades']!r}"
    )
    row = money["trades"][0]
    api_number = row["pnl"] - row["commission"]
    assert api_number == pytest.approx(wallet_delta, abs=1e-9), (
        f"the API would report {api_number!r} but the wallet moved "
        f"{wallet_delta!r}"
    )

    # 3. The guard was told about the close, and about the SAME result.
    #
    #    The close is a PAPER close, so it is booked into the PAPER sandbox
    #    (`rm.paper_stats`). It used to be asserted against `rm.stats`, the
    #    LIVE sandbox -- which is the very defect this branch fixes: a
    #    simulated close was written into the LIVE day's PnL and the LIVE
    #    loss streak, and a paper loss could close the LIVE gate. The
    #    assertions below therefore read the PAPER sandbox and then pin
    #    that the LIVE sandbox never heard about it.
    assert rm.paper_stats.today_pnl > 0, (
        f"the risk manager was told {rm.paper_stats.today_pnl!r}; a winning "
        f"close must not be booked as a loss"
    )
    assert rm.paper_stats.today_pnl == pytest.approx(api_number, rel=0.02), (
        f"the risk manager was told {rm.paper_stats.today_pnl!r} but the API "
        f"and the wallet say {api_number!r}; the guard and the API must read "
        f"the same number"
    )

    # 4. A WIN must not increment the loss streak.
    assert rm.paper_stats.consecutive_losses == 0, (
        f"a winning close must not increment the loss streak, got "
        f"{rm.paper_stats.consecutive_losses}"
    )

    # 5. The LIVE sandbox must not have heard about it at all.
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER close was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
    )
    assert rm.stats.consecutive_losses == 0, (
        f"the PAPER close was counted in the LIVE loss streak "
        f"({rm.stats.consecutive_losses})"
    )

    # 6. No phantom position is left behind in the paper book.
    assert SYMBOL not in paper_executor._positions, (
        f"the paper book still holds {paper_executor._positions.get(SYMBOL)!r}"
    )


@pytest.mark.asyncio
async def test_a_paper_short_closed_by_the_controller_is_not_charged_twice(
    money, monkeypatch
):
    """MUTATION KILLER: the stuck-CLOSING retry must not charge again.

    The close-confirmation block is gated on `supports_positions` too
    (controller.py:15513). While the paper executor did not declare it, the
    controller never confirmed the close, ran all 120 attempts and then gave
    up with "FAILED to confirm position closure" -- leaving the position in
    CLOSING. The retry of that close re-ran `place_order` against an EMPTY
    book: the wallet was charged a second time (a commission with no PnL
    behind it), a second trade row with `pnl=0.0` was written, and the paper
    book was left holding a brand-new phantom position.

    The 0.5 s delay of the retry loop is NOT behaviour under test -- only the
    wallet movements are -- so it is clamped here. Without that, the broken
    path spends 120 attempts x 0.5 s x 2 closes in the suite.
    """
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay, *args, **kwargs):
        return await real_sleep(
            0 if delay and delay > 0 else delay, *args, **kwargs
        )

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    controller, paper_executor, rm = _build_controller(price=90.0)
    _seed_short(paper_executor, controller, entry_price=ENTRY_PRICE, qty=QTY)
    # The PAPER wallet's balance is read first: the PAPER gate is judged
    # against it, and an UNKNOWN number is not judged at all.
    assert await rm.update_balance(mode="paper") is True
    # Pin BOTH sandboxes for the day: this close is a PAPER trade, so it is
    # booked into the PAPER sandbox, and an unpinned PAPER day would roll
    # over and zero the paper day's PnL mid-test.
    for _stats in (rm.stats, rm.paper_stats):
        _stats.last_known_day_str = _utc_day_str()
        _stats.current_balance = 1000.0
        _stats.start_of_day_balance = 1000.0

    await controller.close_position(
        SYMBOL, reason=_CLOSURE_REASON, market_type=MARKET_TYPE
    )
    # The retry: the very same close, run again.
    await controller.close_position(
        SYMBOL, reason=_CLOSURE_REASON, market_type=MARKET_TYPE
    )

    # The wallet was charged ONCE.
    assert len(money["wallet"]) == 1, (
        f"the wallet was charged {len(money['wallet'])} times for one close: "
        f"{money['wallet']!r}"
    )
    wallet_delta = money["wallet"][0]["amount"]
    assert wallet_delta > 0, (
        f"a winning close must credit the wallet, got {wallet_delta!r}"
    )

    # No second, empty close row.
    assert len(money["trades"]) == 1, (
        f"expected exactly one PAPER trade row for one close, got "
        f"{money['trades']!r}"
    )
    zero_rows = [r for r in money["trades"] if float(r.get("pnl") or 0.0) == 0.0]
    assert not zero_rows, (
        f"a second close row with pnl=0.0 was written: {zero_rows!r}"
    )

    # The retry left no phantom position behind in the paper book.
    assert SYMBOL not in paper_executor._positions, (
        f"the paper book still holds {paper_executor._positions.get(SYMBOL)!r}"
    )

    # And the guard was told about the close at all.
    #
    # The close is a PAPER close, so it is booked into the PAPER sandbox. It
    # used to be asserted against `rm.stats`, the LIVE sandbox -- which is
    # the defect this branch fixes (a simulated close written into the LIVE
    # day's PnL and the LIVE loss streak).
    assert rm.paper_stats.today_pnl > 0, (
        f"the risk manager was told {rm.paper_stats.today_pnl!r}; the close "
        f"never reached the risk manager"
    )
    assert rm.paper_stats.today_pnl == pytest.approx(wallet_delta, rel=0.02), (
        f"the risk manager was told {rm.paper_stats.today_pnl!r} but the "
        f"wallet moved {wallet_delta!r}; the retry must not book the close "
        f"twice"
    )
    assert rm.paper_stats.consecutive_losses == 0, (
        f"a winning close must not increment the loss streak, got "
        f"{rm.paper_stats.consecutive_losses}"
    )

    # The LIVE sandbox must not have heard about it.
    assert rm.stats.today_pnl == pytest.approx(0.0, abs=1e-9), (
        f"the PAPER close was booked against the LIVE day's PnL "
        f"({rm.stats.today_pnl!r})"
    )
    assert rm.stats.consecutive_losses == 0, (
        f"the PAPER close was counted in the LIVE loss streak "
        f"({rm.stats.consecutive_losses})"
    )


@pytest.mark.asyncio
async def test_a_new_paper_entry_is_aborted_when_the_paper_book_already_holds_the_symbol(
    money,
):
    """The pre-flight now runs for paper: a duplicate entry is ABORTED.

    With `supports_positions` true, the controller's PRE-FLIGHT API CHECK
    (controller.py:9045-9075) runs for the paper executor too, exactly as it
    does for a live futures account: if the book already holds a position for
    the symbol, the entry is aborted instead of being sent.

    This is a deliberate behaviour change and it is pinned here so it cannot
    change by accident:

      * the paper book holds a LONG on the symbol, but the CONTROLLER has no
        position for it -- the orphan case, where the book and the controller
        disagree. The controller's own "an active position already exists"
        check (controller.py:8410-8415) cannot see it; only the executor's
        book can;
      * before the fix the pre-flight was skipped for paper
        (`supports_positions` was False), so the entry was SENT and the paper
        book was silently scaled in -- a second, unplanned unit of exposure;
      * after the fix the entry is aborted and the book is untouched.

    DCA / grid scale-in is NOT affected: `_execute_dca_grid` places its
    orders directly (`executor.place_order`, controller.py:6197, :6313,
    :6382) and never goes through `_process_signal`.

    NOTE on the log line the pre-flight prints. It says "Adopting found
    position immediately to prevent further spam", and the code underneath
    it only LOGS that -- it does not create the orphan position the comment
    above it promises (controller.py:9066-9072). So the ABORT is real and is
    what this test pins; the "adoption" is a no-op, in the live path too,
    and is deliberately NOT changed here.
    """
    controller, paper_executor, rm = _build_controller(price=110.0)

    # The paper book holds a position the controller does not know about.
    _seed_paper_book_only(paper_executor, entry_price=ENTRY_PRICE, qty=QTY)

    # The controller surface `_process_signal` touches before the pre-flight.
    controller._processing_signal_lock = asyncio.Lock()
    controller._processing_signal_for_symbol = set()
    controller._recent_signals = {}
    controller._signal_throttle_period = 0
    controller._symbol_cooldown_duration = 0
    controller._maybe_release_unverified_emergency_stop = AsyncMock(
        return_value=None
    )
    # The risk manager is not what is under test here: the pre-flight is.
    rm.is_symbol_trading_allowed = AsyncMock(return_value=True)
    rm.assess_signal = AsyncMock(return_value=(True, QTY, 100.0, None))

    strategy_instance = MagicMock()
    strategy_instance.NAME = "TestStrategy"
    controller.running_strategy_instances = {
        "cfg-paper-1": (
            strategy_instance,
            {
                "id": "cfg-paper-1",
                "user_id": USER_ID,
                "mode": "paper",
                "config_data": {"marketType": MARKET_TYPE},
            },
        )
    }

    signal = StrategySignal(
        strategy_name="TestStrategy",
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        stop_loss=95.0,
        take_profit=120.0,
        entry_price=ENTRY_PRICE,
        mode=OrderMode.MARKET,
        trigger_price=110.0,
        details={
            "market_type": MARKET_TYPE,
            "strategy_config_id": "cfg-paper-1",
        },
    )
    pair_info = {
        "market_type": MARKET_TYPE,
        "last_price": 110.0,
        "strategy_config_id": "cfg-paper-1",
    }

    await controller._process_signal(signal, pair_info)

    # The book is untouched: the entry was ABORTED, not scaled in.
    assert paper_executor._positions[SYMBOL]["quantity"] == QTY, (
        f"the paper book was scaled in to "
        f"{paper_executor._positions.get(SYMBOL)!r}; the pre-flight must abort "
        f"a duplicate entry"
    )
    # No order was sent, so no commission was paid and no row was written.
    assert money["wallet"] == [], (
        f"a duplicate entry order was placed and the wallet was charged: "
        f"{money['wallet']!r}"
    )
    assert money["trades"] == [], (
        f"a duplicate entry trade row was written: {money['trades']!r}"
    )
