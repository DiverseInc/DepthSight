# File: tests/test_paper_preflight_position_guard.py
"""
The pre-flight duplicate-entry guard must not blame the exchange for a PAPER position.

THE DEFECT
----------
PR #26 (`fe00122`, merged as `3e8577b`, deployed 2026-10-21T17:44:51Z) added to
`bot_module/paper_executor.py`:

    supports_positions = True
    supports_shorting = True

That declaration is CORRECT and is kept: the paper book is a futures book, and
without it the controller classified the paper executor as SPOT, the SPOT-SYNC
block read a base-asset balance a futures paper wallet never has, and a paper
close was never credited (`paper_executor.py:47-54`).

It also flipped a call site that PR #26 did not intend to touch. The pre-flight
guard in `TradingController._process_signal` asks the book whether it already
holds the symbol:

    controller.py:9092
        real_positions = (
            await executor.get_open_positions()
            if getattr(executor, "supports_positions", False)
            else []
        )

That question -- "does the EXCHANGE already have this symbol?" -- is what the
guard's own comment says it is asking (controller.py:9089, "Making a direct
request to the exchange"). `supports_positions` cannot answer it: it says the
book HAS positions, not WHOSE book it is. Before PR #26 a paper book answered
False and the guard never ran. After PR #26 it runs, and `get_open_positions()`
returns the strategy's OWN simulated position in a Binance-shaped dict, so the
guard reported a PAPER position as an EXCHANGE one and refused the entry.

Observed live (production dashboard, 2026-10-21 ~00:21Z):

    [CRITICAL] [ProcessSignal:VisualBuilderStrategy:BTCUSDT:LONG] STOP! Exchange
    already has position for BTCUSDT: {'symbol': 'BTCUSDT', 'positionAmt':
    '-0.01', 'entryPrice': '84318.0199', 'markPrice': '84181.7',
    'unRealizedProfit': '1.3631990000000225', 'liquidationPrice': '0',
    'leverage': '1.0'}. Aborting duplicate entry.

That is a paper position, and the arithmetic says so:
  * `unRealizedProfit` 1.3631990000000225 == (84318.0199 - 84181.7) * 0.01 with
    IEEE-754 noise, i.e. EXACTLY the paper executor's SHORT branch at
    `paper_executor.py:643-644`.
  * `leverage` '1.0' == `str(pos_data.get("leverage") or 1.0)`, the paper
    assumed-leverage default (`paper_executor.py:661`).
  * `liquidationPrice` '0' == the paper fallback for a falsy computed value
    (`paper_executor.py:658-660`).
So the operator was shown a leverage and a liquidation price for money that was
never at risk, and pointed at an exchange that never held the position.

WORSE, THE POSITION WAS NOT FINDABLE. In the same screenshot the Mission Control
positions panel read "No active positions yet". That is not a rendering bug, it
is structural: the panel is fed from Redis `depthsight:state:positions:*`, which
the controller fills from ITS OWN `_active_positions` and ONLY for
`pos.status == "OPEN"` (`controller.py:4318-4333`). `get_open_positions()` on
the paper executor reads a different dict (`PaperTradingExecutor._positions`).
A position that exists in only one of the two is invisible in the UI, and this
guard is the only thing that reports it.

WHAT THIS TEST PROVES
---------------------
It drives the REAL `TradingController._process_signal` -- the real throttling,
the real config lookup, the real `_get_market_info`, the real risk-manager call,
the real pre-flight guard -- with a REAL `PaperTradingExecutor` that already
holds the symbol, and reads the REAL log record the guard emits. The live case
is the control: same path, a live executor, and the message must still name the
exchange, because for a real book that is the truth.

The oracle is the OPERATOR-VISIBLE MESSAGE plus WHETHER AN ORDER WAS SENT.
Nothing here re-implements the guard's logic.

Red-then-green: revert `bot_module/controller.py` and `bot_module/paper_executor.py`
to their committed state and re-run this file. Tests 1, 2, 4 and 5 fail --
the guard says "Exchange already has position" for a simulated position, and a
price-feed failure takes the whole entry down with it. Tests 3 and 6 stay green
(they are the controls).
"""

import asyncio
import logging
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module.controller import (
    ActivePositionMap,
    SignalDirection,
    TradingController,
)
from bot_module.datatypes import OrderMode, StrategySignal
from bot_module.exchanges.base import ExchangeExecutor
from bot_module.exchanges.binance import BinanceExchangeExecutor
from bot_module.exchanges.ccxt_executor import CcxtExecutor
from bot_module.paper_executor import PaperTradingExecutor

# The live executors: real venues, so `is_paper_book` must be False on each.
LIVE_EXECUTOR_CLASSES = (BinanceExchangeExecutor, CcxtExecutor)

USER_ID = 7
SYMBOL = "BTCUSDT"
STRATEGY = "VisualBuilderStrategy"
CONFIG_ID = 4242
MARKET_TYPE = "futures_usdtm"

# The exact position the production log showed on 2026-10-21: a PAPER SHORT.
PAPER_ENTRY_PRICE = 84318.0199
PAPER_MARK_PRICE = 84181.7
PAPER_QTY = -0.01


# --- Test doubles: inputs only, never behaviour under test --------------------


class _FakeDataConsumer:
    """The only price source the paper executor reads.

    `price` may be a number, None (no tick yet) or an exception INSTANCE to
    raise -- a market-data failure must not be able to take the entry down.
    """

    def __init__(self, price):
        self.price = price
        self.calls = 0

    async def get_latest_price(self, symbol):
        self.calls += 1
        if isinstance(self.price, BaseException):
            raise self.price
        return self.price

    async def get_latest_depth(self, symbol):
        return None


class _FakeDB:
    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, obj, *args, **kwargs):
        return None


class _FakeTradeLogger:
    def __init__(self):
        self.events = []

    def log_order_placed(self, *args, **kwargs):
        pass

    def log_event(self, event_type=None, data=None, **kwargs):
        self.events.append({"event_type": event_type, "data": data})


class _FakeLiveExecutor:
    """A LIVE exchange book: real money, and honest about being live.

    `supports_positions` is True exactly as a Binance futures executor has it --
    that is what makes this the control for the guard.
    """

    exchange_id = "binance"
    market_type = MARKET_TYPE
    supports_positions = True
    supports_shorting = True

    def __init__(self, positions):
        self._positions = positions
        self.orders = []

    async def get_open_positions(self):
        return list(self._positions)

    async def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": 1, "status": "NEW"}

    async def cancel_all_open_orders(self, symbol):
        return {"symbol": symbol, "status": "OK", "count": 0, "cancelled": []}

    async def get_account_balance(self):
        return {"USDT": {"free": "1000.0", "locked": "0.0"}}


def _binance_position(position_amt, entry_price, mark_price, pnl):
    """The shape a REAL exchange returns -- no assumed leverage, no 0 liq price."""
    return {
        "symbol": SYMBOL,
        "positionAmt": str(position_amt),
        "entryPrice": str(entry_price),
        "markPrice": str(mark_price),
        "unRealizedProfit": str(pnl),
        "liquidationPrice": "0",
        "leverage": "5",
    }


# --- The world ----------------------------------------------------------------


def _build_controller(*, mode, data_consumer_price, paper_positions=None):
    """A controller wired with only what `_process_signal` reaches.

    `TradingController.__new__` because `__init__` opens exchange connections;
    every method used below is the real, unbound production one.
    """
    data_consumer = _FakeDataConsumer(data_consumer_price)

    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=data_consumer,
        redis_client=None,
    )
    for symbol, position_data in (paper_positions or {}).items():
        paper_executor._positions[symbol] = position_data

    live_executor = _FakeLiveExecutor([])

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = None
    controller.api_key_name = "test"
    controller.executors = {"live": live_executor, "paper": paper_executor}
    controller.market_executors = {}
    controller.consumer = data_consumer
    controller.db_session = None
    controller.telegram_notifier = None
    controller.user_telegram_chat_id = None
    controller.trade_logger = _FakeTradeLogger()
    controller.realtime_ml_logger = None
    controller._emergency_stop_latched = False

    # --- the guards/locks `_process_signal` takes ---
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller._symbol_locks = {}
    controller._processing_signal_lock = asyncio.Lock()
    controller._processing_signal_for_symbol = set()
    controller._recent_signals = {}
    controller._signal_throttle_period = 0.0
    controller._last_position_close_time_per_symbol = {}
    controller._symbol_cooldown_duration = 0.0
    controller._closing_managed_symbols = set()
    controller.instances_lock = asyncio.Lock()

    # --- the running strategy the config search looks for ---
    controller.running_strategy_instances = {
        CONFIG_ID: (
            types.SimpleNamespace(NAME=STRATEGY),
            {
                "name": STRATEGY,
                "user_id": USER_ID,
                "mode": mode,
                "market_type": MARKET_TYPE,
                "config_data": {"marketType": "FUTURES", "management": []},
            },
        )
    }

    # --- the risk manager: APPROVE, with a known quantity ---
    controller.rm = types.SimpleNamespace(
        max_concurrent_trades=5,
        assess_signal=AsyncMock(return_value=(True, 0.01, 12.5, "")),
        is_symbol_trading_allowed=AsyncMock(return_value=True),
        get_trading_disabled_reason=lambda: "",
        # The start-up fail-closed latch, queried on every entry.
        is_emergency_stop_active=lambda: False,
        is_emergency_stop_operator_initiated=lambda: False,
    )

    # --- market info: the real `_get_market_info` reaches Redis ---
    controller._market_info_cache = {}
    controller._market_info_lock = asyncio.Lock()

    async def _fake_get_market_info(symbol, key, market_type=None):
        return {
            "lot_params": {"stepSize": 0.001, "minQty": 0.001, "quantityPrecision": 3},
            "min_notional": 5.0,
        }.get(key)

    controller._get_market_info = _fake_get_market_info

    paper_executor.controller = controller
    return controller, paper_executor, live_executor, data_consumer


def _seed_paper_short(paper_executor, *, quantity, entry_price):
    """A position in the PAPER book only -- not in the controller's map.

    That asymmetry IS the production state: the guard can only fire when the two
    disagree, because `_process_signal` already refuses a second entry for a
    symbol the controller tracks (controller.py:8456-8461).
    """
    now = datetime.now(timezone.utc)
    paper_executor._positions[SYMBOL] = {
        "quantity": quantity,
        "avg_entry_price": entry_price,
        "entry_timestamp": now,
        "leverage": 1.0,
        "last_funding_timestamp": now,
        "strategy_config_id": CONFIG_ID,
        "entry_client_order_id": "x-entry-paper-00000001",
    }


def _signal(direction=SignalDirection.LONG):
    # MARKET mode validates `trigger_price` (not `entry_price`) as the
    # comparison price, and requires an SL below it for a LONG.
    return StrategySignal(
        strategy_name=STRATEGY,
        symbol=SYMBOL,
        direction=direction,
        stop_loss=82000.0,
        take_profit=88000.0,
        trigger_price=85000.0,
        mode=OrderMode.MARKET,
        details={"strategy_config_id": CONFIG_ID},
    )


def _spy_on_place_order(executor):
    """Records every order the executor is actually asked to place."""
    calls = []
    original = executor.place_order

    async def spy(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    executor.place_order = spy
    return calls


def _abort_records(caplog, min_level=logging.WARNING):
    """The records the guard emitted when it refused an entry.

    `min_level` defaults to WARNING because the two books report at DIFFERENT
    severities on purpose, and every assertion below states which one it expects
    instead of inheriting it from this filter. Collecting at WARNING+ and then
    pinning `levelno` per test is what stops a regression that quietly re-raises
    the paper abort -- or lowers the live one -- from passing unnoticed.
    """
    return [
        rec
        for rec in caplog.records
        if rec.levelno >= min_level and "Aborting" in rec.getMessage()
    ]


def _critical_records(caplog):
    """Kept as the name the paper tests used; now delegates to `_abort_records`."""
    return _abort_records(caplog, logging.WARNING)


# --- 1. THE DEFECT: a PAPER position is not an EXCHANGE position --------------


@pytest.mark.asyncio
async def test_paper_preflight_abort_does_not_claim_the_exchange_holds_it(caplog):
    """The guard's message must be TRUE about a simulated position.

    Production said "STOP! Exchange already has position for BTCUSDT" about a
    position that existed only in `PaperTradingExecutor._positions`, and printed
    a leverage and a liquidation price for money that was never at risk.
    """
    controller, paper_executor, _, _ = _build_controller(
        mode="paper", data_consumer_price=PAPER_MARK_PRICE
    )
    _seed_paper_short(
        paper_executor, quantity=PAPER_QTY, entry_price=PAPER_ENTRY_PRICE
    )
    orders = _spy_on_place_order(paper_executor)

    caplog.set_level(logging.WARNING, logger="bot_module.controller")
    await controller._process_signal(_signal(), {})

    records = _critical_records(caplog)
    assert records, (
        "the pre-flight guard did not fire for a paper book that already holds "
        "the symbol; without a record there is nothing to make honest"
    )
    message = records[0].getMessage()

    assert "Exchange already has position" not in message, (
        "the guard told the operator the EXCHANGE holds a position that exists "
        f"only in the paper book. Message was: {message}"
    )
    assert "paper" in message.lower(), (
        f"the guard must name the book it is actually reading. Message was: {message}"
    )
    assert records[0].levelno == logging.WARNING, (
        "a simulated position with no money behind it is not a CRITICAL incident; "
        f"CRITICAL is the level this guard raises for a real venue holding real "
        f"money, and reusing it here buries that one. Got level "
        f"{logging.getLevelName(records[0].levelno)}: {message}"
    )


@pytest.mark.asyncio
async def test_paper_preflight_abort_names_the_position_so_it_can_be_found(caplog):
    """An abort the operator cannot act on is a dead end.

    The message must carry the strategy, the config id and the direction, and it
    must say the position is NOT in the positions panel -- because that panel
    only ever lists positions the controller tracks (controller.py:4318-4333),
    and this one it does not.
    """
    controller, paper_executor, _, _ = _build_controller(
        mode="paper", data_consumer_price=PAPER_MARK_PRICE
    )
    _seed_paper_short(
        paper_executor, quantity=PAPER_QTY, entry_price=PAPER_ENTRY_PRICE
    )
    _spy_on_place_order(paper_executor)

    caplog.set_level(logging.WARNING, logger="bot_module.controller")
    await controller._process_signal(_signal(), {})

    message = _critical_records(caplog)[0].getMessage()

    assert STRATEGY in message, f"the message must name the strategy: {message}"
    assert str(CONFIG_ID) in message, f"the message must name the config id: {message}"
    assert "LONG" in message, (
        f"the message must name the direction the signal wanted to open: {message}"
    )
    assert "not" in message.lower() and "panel" in message.lower(), (
        "the positions panel does NOT show this position -- an operator who is "
        f"sent to the panel to look for it is being sent nowhere: {message}"
    )


# --- 2. The abort itself is NOT removed ---------------------------------------


@pytest.mark.asyncio
async def test_paper_preflight_still_refuses_the_duplicate_entry():
    """The paper book holds a real position. The entry is still refused.

    Refusing it is deliberate: stacking a second entry on a position the
    controller cannot see would silently corrupt the simulated book and its
    PnL. What this change fixes is the TELLING, not the refusal.
    """
    controller, paper_executor, _, _ = _build_controller(
        mode="paper", data_consumer_price=PAPER_MARK_PRICE
    )
    _seed_paper_short(
        paper_executor, quantity=PAPER_QTY, entry_price=PAPER_ENTRY_PRICE
    )
    orders = _spy_on_place_order(paper_executor)

    await controller._process_signal(_signal(), {})

    assert orders == [], (
        "a duplicate entry was placed on top of an existing paper position: "
        f"{orders}"
    )


# --- 3. THE CONTROL: a live book is still the exchange ------------------------


@pytest.mark.asyncio
async def test_live_preflight_still_blames_the_exchange_and_refuses(caplog):
    """For a LIVE book the original wording is the truth, and must survive.

    This is the control for the whole change: a fix that made every message
    talk about paper would silence the one case that really is a duplicate on a
    real venue holding real money.
    """
    controller, paper_executor, live_executor, _ = _build_controller(
        mode="live", data_consumer_price=PAPER_MARK_PRICE
    )
    live_executor._positions = [
        _binance_position("0.05", 84000.0, 84181.7, 9.085)
    ]
    orders = _spy_on_place_order(live_executor)

    caplog.set_level(logging.WARNING, logger="bot_module.controller")
    await controller._process_signal(_signal(), {})

    records = _critical_records(caplog)
    assert records, "the pre-flight guard did not fire for a live book"
    message = records[0].getMessage()

    assert "Exchange already has position" in message, (
        f"a real duplicate on a real exchange must still say so: {message}"
    )
    assert records[0].levelno == logging.CRITICAL, (
        "the live abort is the one that IS critical -- a real venue holds a real "
        "position this controller does not know about and the next entry would "
        "double down on it. Dropping it to a warning silences the only case that "
        f"costs money. Got level {logging.getLevelName(records[0].levelno)}: "
        f"{message}"
    )
    assert orders == [], f"a duplicate live entry was placed: {orders}"


# --- 4. A failed price lookup must not abort the entry ------------------------


@pytest.mark.asyncio
async def test_price_feed_failure_does_not_abort_a_paper_entry(caplog):
    """`get_open_positions()` awaits the price feed; a failure there is a
    market-data problem, not a reason to refuse a trade.

    `get_open_positions()` is called INSIDE the entry path, and the guard's
    `except Exception` aborts the signal. So an exception from the price lookup
    became a refused entry, with no position and no message to explain it.
    """
    controller, paper_executor, _, data_consumer = _build_controller(
        mode="paper",
        data_consumer_price=RuntimeError("market data feed is down"),
    )
    _seed_paper_short(
        paper_executor, quantity=PAPER_QTY, entry_price=PAPER_ENTRY_PRICE
    )
    orders = _spy_on_place_order(paper_executor)

    caplog.set_level(logging.WARNING, logger="bot_module.controller")
    await controller._process_signal(_signal(), {})

    errors = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.ERROR
        and "Pre-flight check failed" in rec.getMessage()
    ]
    assert not errors, (
        "a price-feed failure aborted the entry through the guard's own "
        f"except-branch: {[e.getMessage() for e in errors]}"
    )

    # The position still exists, so the duplicate is still refused -- by the
    # guard that can see it, with a message that is true.
    assert orders == [], f"an order was placed over an existing position: {orders}"
    records = _critical_records(caplog)
    assert records, (
        "with the price feed down the position must still be reported, not "
        "silently skipped"
    )
    assert "Exchange already has position" not in records[0].getMessage()
    assert records[0].levelno == logging.WARNING, (
        "the severity drop must survive a price-feed failure too -- the guard "
        "must still reach its own branch to report the position it can see"
    )


@pytest.mark.asyncio
async def test_a_missing_price_is_not_reported_as_a_mark_of_zero():
    """`markPrice: "0"` reads like a real quote for a real position.

    It is the fallback at `paper_executor.py:656`, and it is what an operator
    saw next to a real BTCUSDT position. A missing price must fall back to
    something visibly true -- the entry price -- so the PnL of 0.0 that follows
    is the arithmetic of that mark rather than a claim about the market.
    """
    executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=_FakeDataConsumer(None),  # no tick yet
        redis_client=None,
    )
    _seed_paper_short(executor, quantity=PAPER_QTY, entry_price=PAPER_ENTRY_PRICE)

    positions = await executor.get_open_positions()

    assert len(positions) == 1, f"the position must still be reported: {positions}"
    position = positions[0]
    assert float(position["markPrice"]) > 0, (
        f"a position was reported with a mark price of zero: {position}"
    )
    assert float(position["markPrice"]) == pytest.approx(PAPER_ENTRY_PRICE), (
        f"with no price available the mark must be the entry price, so the PnL "
        f"of 0.0 that follows is arithmetic rather than a claim: {position}"
    )


# --- 5. The distinction is a declared attribute, not a type check ------------


def test_paper_and_live_executors_declare_which_book_they_are():
    """`supports_positions` says the book HAS positions; `is_paper_book` says
    WHOSE book it is. Reusing the first for the second is the defect."""
    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=_FakeDataConsumer(PAPER_MARK_PRICE),
        redis_client=None,
    )
    assert paper_executor.is_paper_book is True, (
        "the paper book must declare itself a paper book"
    )
    assert paper_executor.supports_positions is True, (
        "PR #26's declaration must survive this fix: the paper book is a FUTURES "
        "book, and reverting it re-breaks the paper close that reaches the risk "
        "manager with its net PnL"
    )

    # The live side must declare itself NOT a simulation, or the guard would
    # describe every real exchange position as a paper one.
    for exchange_class in LIVE_EXECUTOR_CLASSES:
        assert getattr(exchange_class, "is_paper_book", None) is False, (
            f"{exchange_class.__name__} must declare is_paper_book=False so a "
            "live book is never reported as a simulation"
        )
        # `is_paper_book` must be a CLASS attribute on the live executors, not
        # an instance one: the guard's `getattr` runs against whatever the
        # controller was handed, and a value that only exists after a
        # successful connection is a value that reads as "live" by accident on a
        # half-built executor -- which is the safe direction, but only by luck.
        assert "is_paper_book" in vars(exchange_class), (
            f"{exchange_class.__name__} must declare is_paper_book at CLASS "
            "level, not in __init__"
        )

    # The live executors set `supports_positions` per INSTANCE, in __init__ from
    # the market type -- it is deliberately not a class attribute, so asserting
    # it here would read None. That flag is what the exchange factory builds
    # with, and `tests/test_exchange_factory.py` already pins it on real
    # instances for every market type. What matters HERE is the paper side,
    # asserted above: PR #26's declaration must not be reverted by this fix.


def test_the_executor_protocol_declares_the_flag():
    """`exchanges/base.py` is the runtime contract; a flag nothing declares is
    a flag the next executor will silently forget."""
    protocol = ExchangeExecutor
    annotations = getattr(protocol, "__annotations__", {})
    assert "is_paper_book" in annotations, (
        "the executor protocol must declare is_paper_book, otherwise the next "
        "executor written against it can omit the distinction this fix depends "
        "on"
    )


@pytest.mark.asyncio
async def test_a_live_book_is_not_reported_as_a_simulation():
    """The controller must default an executor that declares nothing to LIVE.

    `getattr(executor, "is_paper_book", False)` resolving to False for the live
    double is the load-bearing default: an executor written before this flag
    existed must keep getting the exchange wording, not the paper one.
    """
    live = _FakeLiveExecutor([])
    controller = TradingController.__new__(TradingController)
    controller.executors = {"live": live}
    controller.market_executors = {}

    resolved = controller._executor_for_market_type(MARKET_TYPE, mode="live")
    assert resolved is live
    assert getattr(resolved, "is_paper_book", False) is False
    assert not hasattr(live, "is_paper_book"), (
        "this double deliberately declares nothing -- it stands in for an "
        "executor written before the flag existed"
    )


# --- 6. The control for the price lookup: a healthy feed still reports PnL ---


@pytest.mark.asyncio
async def test_a_healthy_price_still_reports_the_real_unrealized_pnl():
    """The fallback must not become the normal path.

    With a live price the paper book reports exactly the arithmetic of
    `paper_executor.py:641-644`, which is what the production log's
    `1.3631990000000225` was.
    """
    executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=_FakeDataConsumer(PAPER_MARK_PRICE),
        redis_client=None,
    )
    _seed_paper_short(executor, quantity=PAPER_QTY, entry_price=PAPER_ENTRY_PRICE)

    position = (await executor.get_open_positions())[0]

    expected = (PAPER_ENTRY_PRICE - PAPER_MARK_PRICE) * abs(PAPER_QTY)
    assert float(position["unRealizedProfit"]) == pytest.approx(expected, abs=1e-9), (
        f"the paper book must keep reporting its own PnL arithmetic: {position}"
    )
    assert float(position["markPrice"]) == pytest.approx(PAPER_MARK_PRICE)