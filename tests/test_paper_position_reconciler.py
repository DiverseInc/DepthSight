# File: tests/test_paper_position_reconciler.py
"""
A paper position the controller never learned about must be RECOVERED, not left
to stall its symbol forever.

THE DEFECT
----------
`_process_signal`'s pre-flight duplicate-entry guard reads the EXECUTOR's book.
`PaperTradingExecutor.supports_positions` is True (`paper_executor.py:55`), so
the check runs for paper exactly as it does for a live futures account: if the
book already holds a position for the symbol, the entry is aborted -- correctly,
since the existing size and leverage are unknown.

The block under that abort logged "Adopting found position immediately to prevent
further spam" and then wrote NOTHING (`git log -L` on that range returns only
`8b0858d` "feat(bot): initial bot module" -- the write was never there; this is an
implement, not a restore). So the book kept a position the controller's map never
learned, and every later signal for that symbol aborted at the same guard. The
symbol could not trade again until a restart emptied the in-memory book.

Nothing else repaired the divergence:
  * `_reconcile_positions_with_exchange` DOES adopt orphans, but it hardcoded
    `executor = self.executors.get("live")` and returned early when that was
    absent. A paper-only controller has no live executor BY DESIGN
    (`api_key_id is None`), so it never reconciled at all.
  * `_notify_controller_about_fill` explicitly declines to adopt.
  * `_ensure_paper_exit_orders_present` walks the CONTROLLER's map, so it cannot
    discover a book entry the controller never learned.

THE FIX
-------
The reconciler selects its executor by mode instead of assuming "live", so a
paper-only controller reconciles its paper book on the same 60s timer and at
start-up, reusing the EXISTING tested adoption path. The dead log at the guard
becomes a truthful one that names the reconciler. Nothing is written under the
global `_positions_dict_lock` -- `_build_adopted_position` does a DB query and a
`get_open_orders` call, which is why the reconciler does that work outside it.

WHY THIS TEST SHAPE
-------------------
Paper-only controller: `api_key_id is None`, a real `PaperTradingExecutor`, and
NO "live" key in `executors` at all -- the exact production configuration, where
the hardcoded `executors.get("live")` returned None and the method gave up.

The divergence is seeded with real `paper_executor.place_order(...)` calls rather
than by poking `_positions`, so the book is written by the same code that writes
it in production and the controller genuinely never learns about it.

The resting STOP_MARKET is seeded alongside the entry because that IS the
production shape: the controller places the entry and its stop, then loses the
position from its map. `_build_adopted_position` picks the resting stop back up
from the order book, which is what makes the adopted position eligible for exit
arming at all -- an adopted position with no stop target cannot be protected by a
resting order, by design (`_position_has_active_stop_target`).
"""

import asyncio
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_module.controller import (
    ActivePositionMap,
    LivePosition,
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
STRATEGY_CONFIG_ID = "cfg-paper-1"

PRICE = 110.0
QTY = 1.0
STOP_LOSS = 95.0

ENTRY_CLIENT_ORDER_ID = "x-entry-orphan-reconcile-01"
SL_CLIENT_ORDER_ID = "x-sl-orphan-reconcile-0001"


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
    """The paper executor's whole DB surface."""

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


class _FakeLiveExecutor:
    """A real exchange connection, for the live-path controls only."""

    market_type = MARKET_TYPE

    def __init__(self, exchange_positions):
        self._exchange_positions = exchange_positions

    async def get_open_positions(self):
        return self._exchange_positions

    async def get_open_orders(self, symbol=None):
        return []


class _FakeRedis:
    async def get(self, key):
        return None

    async def set(self, key, value):
        return True

    async def zadd(self, *args, **kwargs):
        return 1

    async def zremrangebyscore(self, *args, **kwargs):
        return 0


# --- The MONEY: the oracle ----------------------------------------------------


@pytest.fixture
def paper_world(monkeypatch):
    """Counts every order the paper book is asked to place, and the wallet rows.

    `orders` is the oracle for "zero orders placed": it wraps the REAL
    `place_order` and delegates to it, so the count is of orders that genuinely
    went through the executor rather than of calls that were intercepted.
    """
    record = {"wallet": [], "trades": [], "orders": []}

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
            types.SimpleNamespace(asset="USDT", balance=1000.0),
        ]

    async def _fake_get_last_open_trade_for_symbol(db, user_id, symbol):
        # An orphan created by a crash may have no surviving open-trade row, so
        # the real adoption path falls back to strategy="Unknown/Manual".
        return None

    monkeypatch.setattr(
        crud, "update_paper_wallet_balance", _fake_update_paper_wallet_balance
    )
    monkeypatch.setattr(crud, "create_trade", _fake_create_trade)
    monkeypatch.setattr(crud, "get_paper_wallet", _fake_get_paper_wallet)
    monkeypatch.setattr(
        crud,
        "get_last_open_trade_for_symbol",
        _fake_get_last_open_trade_for_symbol,
    )

    # `place_order` imports the analytics task lazily. A celery app is noise in
    # a unit test, not behaviour under test.
    fake_tasks = types.ModuleType("tasks")
    fake_tasks.process_live_trade_analytics_task = MagicMock()
    monkeypatch.setitem(sys.modules, "tasks", fake_tasks)

    return record


def _count_orders(paper_executor, monkeypatch, record):
    """Wrap the REAL place_order so every order it places is recorded."""
    real_place_order = paper_executor.place_order

    async def _spy_place_order(*args, **kwargs):
        record["orders"].append({"args": args, "kwargs": kwargs})
        return await real_place_order(*args, **kwargs)

    monkeypatch.setattr(paper_executor, "place_order", _spy_place_order)


# --- The world ----------------------------------------------------------------


def _build_controller(price: float, *, api_key_id=None, live_executor=None):
    """A real TradingController, paper-only by default.

    With the defaults (`api_key_id is None`, no "live" key in `executors`)
    this is the PAPER-ONLY production configuration -- and exactly the state
    in which `_reconcile_positions_with_exchange`'s hardcoded
    `executors.get("live")` returned None and the method gave up.

    `api_key_id` / `live_executor` let the LIVE controls below build the two
    other real shapes: a live controller WITH a live executor, and a live
    controller whose live executor FAILED TO BUILD.

    Built with `__new__` because `TradingController.__init__` opens exchange
    connections; every method used below is the real, unbound production one.
    """
    data_consumer = _FakeDataConsumer(price)
    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=data_consumer,
        redis_client=None,
    )

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = api_key_id
    controller.api_key_name = "paper" if api_key_id is None else "live"
    # Paper-only means NO "live" key at all. The live controls pass one.
    controller.executors = {"paper": paper_executor}
    if live_executor is not None:
        controller.executors["live"] = live_executor
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
    controller._monitored_symbols = set()
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {}

    # The reconciler's strategy-attribution DB lookup. Yields nothing, so the
    # real `_build_adopted_position` code path runs and falls through to its
    # "Unknown/Manual" default exactly as it would in production.
    async def _fake_get_db_session():
        return
        yield  # pragma: no cover -- makes this an async generator

    controller.get_db_session = _fake_get_db_session

    # Data-stream subscription management is not under test here and reaches for
    # consumer/redis internals a unit test should not have to build.
    controller._update_monitored_symbols = AsyncMock(return_value=None)

    # The surface `_process_signal` touches before the pre-flight guard.
    controller._processing_signal_lock = asyncio.Lock()
    controller._processing_signal_for_symbol = set()
    controller._recent_signals = {}
    controller._signal_throttle_period = 0
    controller._symbol_cooldown_duration = 0
    controller._maybe_release_unverified_emergency_stop = AsyncMock(return_value=None)

    controller.rm = RiskManager(
        executor=live_executor,
        paper_executor=paper_executor,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    # The risk manager is not what is under test here; the pre-flight is.
    controller.rm.is_symbol_trading_allowed = AsyncMock(return_value=True)
    controller.rm.assess_signal = AsyncMock(return_value=(True, QTY, 100.0, None))
    paper_executor.controller = controller

    return controller, paper_executor


def _running_strategy():
    strategy_instance = MagicMock()
    strategy_instance.NAME = "TestStrategy"
    return {
        STRATEGY_CONFIG_ID: (
            strategy_instance,
            {
                "id": STRATEGY_CONFIG_ID,
                "user_id": USER_ID,
                "mode": "paper",
                "config_data": {"marketType": MARKET_TYPE},
            },
        )
    }


def _signal():
    return StrategySignal(
        strategy_name="TestStrategy",
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        stop_loss=STOP_LOSS,
        take_profit=120.0,
        entry_price=PRICE,
        mode=OrderMode.MARKET,
        trigger_price=PRICE,
        details={
            "market_type": MARKET_TYPE,
            "strategy_config_id": STRATEGY_CONFIG_ID,
        },
    )


def _pair_info():
    return {
        "market_type": MARKET_TYPE,
        "last_price": PRICE,
        "strategy_config_id": STRATEGY_CONFIG_ID,
    }


async def _seed_divergence(paper_executor):
    """Writes a position into the paper book through the REAL producer.

    Deliberately bypasses the controller (no `_process_signal`, no
    `_notify_controller_about_fill`), which is how the book and the controller's
    map come to disagree: `place_order` writes `self._positions[symbol]` and the
    controller's map never learns about it.
    """
    await paper_executor.place_order(
        SYMBOL,
        "BUY",
        "MARKET",
        quantity=QTY,
        newClientOrderId=ENTRY_CLIENT_ORDER_ID,
        entry_client_order_id=ENTRY_CLIENT_ORDER_ID,
        strategy_config_id=STRATEGY_CONFIG_ID,
        signal_details={"market_type": MARKET_TYPE},
    )
    # The stop the controller would have placed with the entry.
    await paper_executor.place_order(
        SYMBOL,
        "SELL",
        "STOP_MARKET",
        stopPrice=STOP_LOSS,
        quantity=QTY,
        newClientOrderId=SL_CLIENT_ORDER_ID,
        strategy_config_id=STRATEGY_CONFIG_ID,
    )


# --- The defect --------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_orphan_is_recovered_by_the_reconciler_and_does_not_stall(
    paper_world, monkeypatch
):
    """The full stall-to-recovery cycle, asserted end to end.

    RED on unfixed `main` at step (2): the reconciler hardcoded
    `executors.get("live")`, found None in this paper-only controller, logged
    "Skipping reconciliation" and returned. Nothing was ever adopted, so
    `_active_position_get` stayed None and the symbol stayed stalled forever.

    GREEN after the fix: one reconcile run adopts the orphan with `mode="paper"`
    and `api_key_id=None`, which is what makes the resting stop discoverable and
    the position visible to the exit-arming pass.
    """
    controller, paper_executor = _build_controller(price=PRICE)
    controller.running_strategy_instances = _running_strategy()
    _count_orders(paper_executor, monkeypatch, paper_world)

    await _seed_divergence(paper_executor)

    # --- precondition: the divergence is real ------------------------------
    assert SYMBOL in paper_executor._positions, "the seed must write the book"
    assert paper_executor._positions[SYMBOL]["quantity"] == pytest.approx(QTY)
    assert controller._active_position_get(SYMBOL, MARKET_TYPE) is None, (
        "precondition: the controller must NOT know about the book position"
    )
    assert controller.executors.get("live") is None, (
        "precondition: this is a paper-only controller"
    )

    # Everything measured from here on must place ZERO orders.
    paper_world["orders"].clear()
    paper_world["wallet"].clear()
    paper_world["trades"].clear()
    book_qty_before = paper_executor._positions[SYMBOL]["quantity"]

    # --- (1) the first signal aborts, placing zero orders -------------------
    await controller._process_signal(_signal(), _pair_info())

    assert paper_world["orders"] == [], (
        f"the first signal placed orders: {paper_world['orders']!r}; the "
        f"pre-flight must abort a duplicate entry"
    )
    assert paper_world["wallet"] == [], f"wallet charged: {paper_world['wallet']!r}"
    assert paper_world["trades"] == [], f"trade rows written: {paper_world['trades']!r}"
    assert paper_executor._positions[SYMBOL]["quantity"] == pytest.approx(
        book_qty_before
    ), "the book was scaled in by a duplicate entry"

    # --- (2) ONE reconcile run adopts the orphan ----------------------------
    await controller._reconcile_positions_with_exchange()

    adopted = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert adopted is not None, (
        "the paper orphan was NOT adopted. Before the fix the reconciler "
        "hardcoded executors.get('live'), which is None on a paper-only "
        "controller, so it returned without doing anything and the symbol "
        "stayed stalled until a restart emptied the paper book."
    )
    assert adopted.mode == "paper", (
        f"adopted with mode={adopted.mode!r}; a paper position must resolve "
        f"executors.get(position.mode) to executors['paper'], not None"
    )
    assert adopted.api_key_id is None, (
        f"adopted with api_key_id={adopted.api_key_id!r}; "
        f"_ensure_paper_exit_orders_present keys its paper handling off None"
    )
    assert adopted.status == "OPEN"
    assert adopted.remaining_quantity == pytest.approx(QTY)

    # --- (3) the second signal still places zero orders ---------------------
    controller._processing_signal_for_symbol = set()
    paper_world["orders"].clear()

    await controller._process_signal(_signal(), _pair_info())

    assert paper_world["orders"] == [], (
        f"the second signal placed orders: {paper_world['orders']!r}; "
        f"adoption must not unblock a DUPLICATE entry"
    )
    assert paper_executor._positions[SYMBOL]["quantity"] == pytest.approx(
        book_qty_before
    ), "the book was stacked by a second entry"

    # --- (4) exit arming leaves a real STOP_MARKET resting ------------------
    await controller._ensure_paper_exit_orders_present(reason="periodic")

    open_orders = await paper_executor.get_open_orders(SYMBOL)
    stops = [
        o for o in open_orders if str(o.get("type", "")).upper() == "STOP_MARKET"
    ]
    assert len(stops) == 1, (
        f"expected exactly one resting STOP_MARKET for {SYMBOL}, found "
        f"{len(stops)} in {[o.get('type') for o in open_orders]!r}"
    )
    stop = stops[0]
    assert float(stop["stopPrice"]) == pytest.approx(STOP_LOSS)
    assert stop["side"] == "SELL", "a LONG stop-loss must rest as a SELL"
    assert float(stop["origQty"]) == pytest.approx(QTY)

    # The adopted position must point at that same live order -- i.e. the
    # adoption picked the resting stop back up out of the paper book, which is
    # what made the position armable at all.
    adopted = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert adopted.current_sl_price == pytest.approx(STOP_LOSS), (
        f"adopted current_sl_price={adopted.current_sl_price!r}; without a stop "
        f"target the position cannot be protected by a resting order"
    )
    assert adopted.current_sl_order_id == stop["orderId"]


# --- The live path must NOT change -------------------------------------------
#
# The fix adds a paper fallback to the reconciler. These two controls pin that the
# fallback is reachable ONLY for a paper-only controller, because the "close what
# is not in this book" half of the method is destructive against the wrong book.


@pytest.mark.asyncio
async def test_live_controller_reconciles_live_book_and_never_the_paper_book(
    paper_world, monkeypatch
):
    """A controller holding BOTH executors reconciles only its live book.

    The live book reports an orphan on a different symbol; the paper book holds a
    position too. The reconciler must adopt the live orphan as `mode="live"` and
    must not touch the paper book's entry.
    """
    live_orphan = {
        "symbol": "ETHUSDT",
        "positionAmt": "2.0",
        "entryPrice": "2000.0",
    }
    controller, paper_executor = _build_controller(
        price=PRICE, api_key_id=42, live_executor=_FakeLiveExecutor([live_orphan])
    )
    _count_orders(paper_executor, monkeypatch, paper_world)

    # The paper book holds SYMBOL; the live book does NOT.
    await paper_executor.place_order(
        SYMBOL,
        "BUY",
        "MARKET",
        quantity=QTY,
        newClientOrderId=ENTRY_CLIENT_ORDER_ID,
        entry_client_order_id=ENTRY_CLIENT_ORDER_ID,
        strategy_config_id=STRATEGY_CONFIG_ID,
        signal_details={"market_type": MARKET_TYPE},
    )

    await controller._reconcile_positions_with_exchange()

    adopted_live = controller._active_position_get("ETHUSDT", MARKET_TYPE)
    assert adopted_live is not None, "the live orphan must still be adopted"
    assert adopted_live.mode == "live", (
        f"live orphan adopted with mode={adopted_live.mode!r}"
    )
    assert adopted_live.api_key_id == 42

    assert controller._active_position_get(SYMBOL, MARKET_TYPE) is None, (
        "the paper book was reconciled by a controller that has a live executor; "
        "the paper book must be left alone while a live book is present"
    )


@pytest.mark.asyncio
async def test_live_controller_without_a_live_executor_does_not_reconcile_paper(
    paper_world, monkeypatch
):
    """The destructive case: a LIVE controller must never fall back to paper.

    `api_key_id` is set but the live executor failed to build, so `executors`
    has no "live" key. The paper book holds a position on a DIFFERENT
    symbol, so the controller's LIVE position for SYMBOL is ABSENT
    from that book.

    If the reconciler fell back to the paper book, the live position would
    fall into the "exists internally but NOT on exchange" branch and be
    marked CLOSED -- a real live position closed by a bot that was only
    talking to a simulator.

    Seeding the SAME symbol in the paper book would be a weaker test: it
    lands in the to_update branch instead, which never closes anything.
    This test was strengthened after the mutation guard reported that
    mutant "drop the api_key_id gate" SURVIVED against the same-symbol setup.

    It is that mutant which makes the paper fallback dangerous, and it is
    why the fallback is gated on `api_key_id is None` and not merely on
    "no live executor".
    """
    controller, paper_executor = _build_controller(
        price=PRICE, api_key_id=42, live_executor=None
    )
    _count_orders(paper_executor, monkeypatch, paper_world)

    # A real LIVE position the controller is managing.
    live_position = LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        entry_price=PRICE,
        initial_quantity=QTY,
        remaining_quantity=QTY,
        entry_time=time.time() - 3600,
        strategy="TestStrategy",
        status="OPEN",
        mode="live",
        market_type=MARKET_TYPE,
        api_key_id=42,
        user_id=USER_ID,
        config_id=STRATEGY_CONFIG_ID,
    )
    controller._active_position_set(live_position)

    # The paper book holds a DIFFERENT symbol, so the live position above
    # is ABSENT from it -- which is what puts it on the destructive path.
    await paper_executor.place_order(
        "ETHUSDT",
        "BUY",
        "MARKET",
        quantity=QTY,
        newClientOrderId=ENTRY_CLIENT_ORDER_ID,
        entry_client_order_id=ENTRY_CLIENT_ORDER_ID,
        strategy_config_id=STRATEGY_CONFIG_ID,
        signal_details={"market_type": MARKET_TYPE},
    )
    assert "ETHUSDT" in paper_executor._positions
    assert SYMBOL not in paper_executor._positions, (
        "precondition: the live position symbol must be ABSENT from the "
        "paper book, or the reconciler takes the to_update path and "
        "never reaches the destructive to_close branch"
    )

    await controller._reconcile_positions_with_exchange()

    survivor = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert survivor is not None, (
        "a live controller with no live executor reconciled against the "
        "paper book and closed its own live position"
    )
    assert survivor.status == "OPEN", (
        f"a LIVE position was marked status={survivor.status!r} "
        f"exit_reason={survivor.exit_reason!r} by reconciling against the "
        f"paper book; the paper fallback must be gated on api_key_id is None"
    )
    assert survivor.exit_reason is None

    # And the paper book's own orphan must not have been adopted either.
    assert controller._active_position_get("ETHUSDT", MARKET_TYPE) is None, (
        "a live controller adopted an orphan from the PAPER book; the paper "
        "fallback must be gated on api_key_id is None"
    )

# --- The regression this branch's first cut actually introduced -------------
#
# Found by the `dsh` reviewer, then verified against the code rather than
# believed. `_load_runtime_state` restores EVERY persisted position unfiltered
# when there is no live executor to verify against (controller.py:4017-4019,
# `validated_positions = restored_positions_objects`) -- including positions with
# mode="live" and a real api_key_id. The reconciler's snapshot filter is
# market_type ONLY, never mode or api_key_id, so on a paper-only controller a
# restored LIVE position lands in `to_close` (it is absent from the paper book)
# and is marked CLOSED with exit_reason="CLOSED_WHILE_OFFLINE".
#
# Before this branch the early return at the executor selection made that
# unreachable. The paper fallback made it reachable, so it is this branch's bug,
# not a pre-existing one.


@pytest.mark.asyncio
async def test_restored_live_position_is_not_closed_by_a_paper_reconcile(
    paper_world, monkeypatch
):
    # A live position restored onto a paper-only controller must survive.
    #
    # Reproduces the real sequence: the user had live positions, then the API
    # key was removed or disabled, so the controller is rebuilt with
    # api_key_id is None. Start-up restores every persisted position because
    # there is no live executor to verify against, so a mode="live" position is
    # in the map. Before this branch the reconciler gave up at the executor
    # selection and the position was never touched.
    #
    # The paper book does not hold this symbol, so without a mode-aware
    # snapshot the position is "in the controller but not in this book" and gets
    # closed -- a real live position destroyed by a bot talking to a simulator.
    controller, paper_executor = _build_controller(price=PRICE)
    _count_orders(paper_executor, monkeypatch, paper_world)

    # What `_load_runtime_state` restores unfiltered on a controller with no
    # live executor: a position whose mode is "live" and whose api_key_id is a
    # real key id.
    restored_live = LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        entry_price=PRICE,
        initial_quantity=QTY,
        remaining_quantity=QTY,
        entry_time=time.time() - 3600,
        strategy="TestStrategy",
        status="OPEN",
        mode="live",
        market_type=MARKET_TYPE,
        api_key_id=42,
        user_id=USER_ID,
        config_id=STRATEGY_CONFIG_ID,
    )
    controller._active_position_set(restored_live)

    # The paper book does NOT hold this symbol.
    assert SYMBOL not in paper_executor._positions

    await controller._reconcile_positions_with_exchange()

    survivor = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert survivor is not None, (
        "a restored LIVE position was removed by a PAPER reconcile; the "
        "reconciler's internal snapshot must be filtered by mode/api_key_id, "
        "not by market_type alone"
    )
    assert survivor.status == "OPEN", (
        f"a restored LIVE position was closed by a paper reconcile: "
        f"status={survivor.status!r} exit_reason={survivor.exit_reason!r}. "
        f"Before this branch the early return made this unreachable."
    )
    assert survivor.api_key_id == 42, "the restored position was mutated"

# --- The residual hole the second review round found -------------------------
#
# `LivePosition.api_key_id` DEFAULTS TO None (controller.py:362) and
# `_load_runtime_state` backfills a missing one with `self.api_key_id`
# (controller.py:3978-3979) -- which is itself None on a paper-only controller.
# So a `mode="live"` position persisted before that field existed comes back with
# `api_key_id is None`, and the api_key_id-only snapshot filter let it straight
# into `to_close`. Found by `dsh` in the second review round; verified against
# the code rather than believed.


@pytest.mark.asyncio
async def test_legacy_live_position_with_no_api_key_id_is_not_closed_by_a_paper_reconcile(
    paper_world, monkeypatch
):
    # A legacy live position carries mode="live" but api_key_id is None.
    #
    # This is the shape `_load_runtime_state` produces for any `mode="live"`
    # position persisted before `api_key_id` existed on the dataclass: the field
    # defaults to None and the backfill writes `self.api_key_id`, which on a
    # paper-only controller is None too. Filtering the snapshot on api_key_id
    # ALONE therefore does not exclude it, and it lands in `to_close` because it
    # is absent from the paper book.
    controller, paper_executor = _build_controller(price=PRICE)
    _count_orders(paper_executor, monkeypatch, paper_world)

    legacy_live = LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        entry_price=PRICE,
        initial_quantity=QTY,
        remaining_quantity=QTY,
        entry_time=time.time() - 3600,
        strategy="TestStrategy",
        status="OPEN",
        mode="live",
        market_type=MARKET_TYPE,
        # Exactly what the dataclass default + the None backfill produce.
        api_key_id=None,
        user_id=USER_ID,
        config_id=STRATEGY_CONFIG_ID,
    )
    controller._active_position_set(legacy_live)

    assert SYMBOL not in paper_executor._positions

    await controller._reconcile_positions_with_exchange()

    survivor = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert survivor is not None, (
        "a legacy mode='live' position with api_key_id=None was removed by a "
        "PAPER reconcile; the snapshot filter must check mode as well as "
        "api_key_id, because api_key_id alone is None for legacy positions"
    )
    assert survivor.status == "OPEN", (
        f"a legacy LIVE position was closed by a paper reconcile: "
        f"status={survivor.status!r} exit_reason={survivor.exit_reason!r}"
    )


@pytest.mark.asyncio
async def test_a_reconcile_mode_that_contradicts_the_executor_is_refused(
    paper_world, monkeypatch
):
    # The executor and the mode may never disagree.
    #
    # A caller that passes `reconcile_mode="live"` together with the paper
    # executor would bypass the mode-aware snapshot filter and run the to_close
    # half against a simulator's book -- closing real live positions. Rather
    # than resolve the conflict in favour of one argument, the method refuses.
    controller, paper_executor = _build_controller(price=PRICE)
    _count_orders(paper_executor, monkeypatch, paper_world)

    with pytest.raises(ValueError, match="contradicts the executor"):
        await controller._reconcile_positions_with_exchange(
            executor=paper_executor, reconcile_mode="live"
        )
