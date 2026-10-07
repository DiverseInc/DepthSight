# File: tests/test_paper_exit_orders_survive_restart.py
"""
A paper position's resting exit orders must exist again after a bot restart.

THE DEFECT
----------
`PaperTradingExecutor` keeps its resting SL / partial-TP orders in
`self._open_orders`, a plain dict created empty in `__init__` and never
persisted or rebuilt. On restart the bot restores the POSITION from Redis
*including* its persisted exit-order state (`sl_placement_initiated`,
`current_sl_order_id`, `ptp_placement_initiated_flags`, each partial TP's
`order_id`/`client_order_id` with status PENDING) -- but the orders themselves
are gone.

Every consumer of that state believes the orders exist:
  - `_place_stop_loss` returns True on a stale `current_sl_order_id` having
    placed nothing;
  - `_place_partial_tp` returns early on `ptp_placement_initiated_flags`;
  - `_check_and_close_positions_without_sl` skips any position whose
    `sl_placement_initiated` is True, so the safety net that would otherwise
    force-close an unprotected position is disabled by the same stale flag.

Observed live: BTCUSDT LONG opened 2026-10-05T22:47:01Z, SL 84802.88,
TP 88201.54; the bot restarted 2026-10-07T00:02:18Z; mark fell to 84268.3 --
below the stop on every sample -- and the position never closed.

WHY THIS TEST SHAPE
-------------------
It drives the REAL `_load_runtime_state` and the REAL
`_rearm_paper_exit_orders_after_restart` against a REAL `PaperTradingExecutor`
and a REAL `RiskManager`, and it asserts the OUTCOME: an order is actually
resting in `paper_executor._open_orders` and the real `check_open_orders()`
actually consumes it once price crosses. No production logic is copied here and
`tick_size` is never injected -- a paper-only controller genuinely has an empty
market-info cache, so `_place_stop_loss` takes its real `DEFAULT_TICK_SIZE`
fallback path, which is exactly what happens in production.

The only stub is `PaperTradingExecutor.place_order` in the fill test, and only
to keep the settlement/DB write out of a unit test. The trigger logic under
claim -- scanning `_open_orders`, parsing the stop price, comparing against the
mark -- is the real, unmodified `check_open_orders`.
"""

import asyncio
import json
import time

import pytest

from bot_module.controller import (
    ActivePositionMap,
    LivePosition,
    PartialTpOrderInfo,
    SignalDirection,
    TradingController,
)
from bot_module.paper_executor import PaperTradingExecutor
from bot_module.risk_manager import RiskManager

USER_ID = 7
SYMBOL = "BTCUSDT"

ENTRY_PRICE = 85000.0
STOP_LOSS = 84802.88209956
TAKE_PROFIT = 88201.54300088
REMAINING_QTY = 0.05

# Above the stop at placement time (so `_place_stop_loss`'s pre-flight passes),
# then driven below it to make the restored order trigger.
PRICE_AT_RESTART = 86000.0
PRICE_AFTER_STOP_CROSSED = 84000.0


# --- Test doubles: inputs only, never behaviour under test --------------------


class _FakeDataConsumer:
    """The only price source the paper executor reads."""

    def __init__(self, price: float):
        self.price = price

    async def get_latest_price(self, symbol):
        return self.price

    async def get_latest_depth(self, symbol):
        return None


class _FakeRedis:
    """Serves the persisted runtime-state snapshot the previous process wrote."""

    def __init__(self, snapshot: dict):
        self._snapshot = snapshot

    async def get(self, key):
        return json.dumps(self._snapshot)


class _FakeLiveExecutor:
    """Stands in for a real exchange connection on the live-controller test only."""

    market_type = "futures_usdtm"

    def __init__(self, exchange_positions):
        self._exchange_positions = exchange_positions

    async def get_open_positions(self):
        return self._exchange_positions


# --- Fixture data ------------------------------------------------------------


def _persisted_position(**overrides) -> LivePosition:
    """A position exactly as `_publish_state_to_redis` would have persisted it.

    Every exit-order field carries a plausible, already-stale id: those orders
    only ever existed in the previous process's in-memory order book.
    """
    position = LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        entry_price=ENTRY_PRICE,
        initial_quantity=REMAINING_QTY,
        remaining_quantity=REMAINING_QTY,
        entry_time=time.time() - 3600,
        status="OPEN",
        mode="paper",
        api_key_id=None,
        user_id=USER_ID,
        strategy="TestStrategy",
        config_id=6999,
        market_type="futures_usdtm",
        signal_details={},
        entry_client_order_id="x-entry-abc12345",
        entry_order_id="2200861597",
        entry_order_status="FILLED",
        initial_stop_loss=STOP_LOSS,
        current_sl_price=STOP_LOSS,
        initial_take_profit=TAKE_PROFIT,
        # --- the stale exit-order state a restart must not trust ---
        current_sl_order_id="2200861599",
        current_sl_client_order_id="x-sl-deadbeefdeadbeef",
        is_sl_algo_order=False,
        sl_placement_initiated=True,
        ptp_placement_initiated_flags={0: True},
        partial_tp_orders=[
            PartialTpOrderInfo(
                target_price=TAKE_PROFIT,
                orig_fraction=0.5,
                quantity=REMAINING_QTY / 2,
                order_id=2200861598,
                client_order_id="x-ptp-cafebabe1234567",
                status="PENDING",
            )
        ],
        # Opened an hour ago, so the missing-SL grace period has long expired
        # and re-arming after the main loop's first tick would market-close it.
        time_status_open=time.time() - 3600,
    )
    for key, value in overrides.items():
        setattr(position, key, value)
    return position


def _state_snapshot(position: LivePosition) -> dict:
    """Serialised through the REAL producer, `LivePosition.to_dict`."""
    return {
        "serialization_format": "json",
        "timestamp": time.time(),
        "monitored_symbols": [SYMBOL],
        "closing_managed_symbols": [],
        "last_known_symbols": [SYMBOL],
        "currently_managed_symbols": [SYMBOL],
        "active_positions": {"paper:BTCUSDT": position.to_dict()},
    }


# --- Controller wiring -------------------------------------------------------


def _build_controller(persisted_position, live_executor=None, price=PRICE_AT_RESTART):
    """A real TradingController with the attribute surface startup touches.

    Built with `__new__` because `TradingController.__init__` opens exchange
    connections; every method used below is the real, unbound production one.
    """
    data_consumer = _FakeDataConsumer(price)
    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=None,
        data_consumer=data_consumer,
        redis_client=None,
    )

    api_key_id = None if live_executor is None else 42

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = api_key_id
    controller.api_key_name = "paper" if live_executor is None else "live"
    controller.executors = {"live": live_executor, "paper": paper_executor}
    controller.market_executors = {}
    controller.consumer = data_consumer
    controller.redis_client = _FakeRedis(_state_snapshot(persisted_position))
    controller.redis_key_runtime_state = f"runtime_state:{USER_ID}"
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller._symbol_locks = {}
    controller._market_info_cache = {}
    controller._market_info_lock = asyncio.Lock()
    controller.telegram_notifier = None
    controller.user_telegram_chat_id = None
    controller.rm = RiskManager(
        executor=live_executor,
        paper_executor=paper_executor,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    paper_executor.controller = controller

    return controller, paper_executor, data_consumer


async def _restart(controller) -> LivePosition:
    """Simulates the restart: restore from Redis, then re-arm exit orders."""
    await controller._load_runtime_state()
    await controller._rearm_paper_exit_orders_after_restart()
    return controller._active_position_get(SYMBOL, "futures_usdtm")


# --- The defect --------------------------------------------------------------


@pytest.mark.asyncio
async def test_restored_paper_position_keeps_a_real_resting_stop_loss():
    """The core regression: a real STOP_MARKET order must be resting again.

    Without the fix, restore keeps `sl_placement_initiated=True` and the stale
    `current_sl_order_id`, so `_place_stop_loss` short-circuits and
    `_open_orders` stays empty forever.
    """
    controller, paper_executor, _ = _build_controller(_persisted_position())

    # The paper order book starts empty in a fresh process -- that is the bug.
    assert paper_executor._open_orders == {}

    position = await _restart(controller)

    stops = [o for o in paper_executor._open_orders.values() if o["type"] == "STOP_MARKET"]
    assert len(stops) == 1, (
        f"expected exactly one resting stop-loss after the restart, found "
        f"{len(stops)} in {list(paper_executor._open_orders.values())}"
    )

    stop = stops[0]
    assert stop["symbol"] == SYMBOL
    assert stop["side"] == "SELL", "a LONG stop-loss must rest as a SELL"
    assert float(stop["stopPrice"]) == pytest.approx(STOP_LOSS, rel=1e-9)
    assert float(stop["origQty"]) == pytest.approx(REMAINING_QTY, rel=1e-9)

    # The position must now point at an order that actually exists, rather than
    # at the id of one that died with the previous process.
    assert position.current_sl_order_id is not None
    assert position.current_sl_order_id != "2200861599"


@pytest.mark.asyncio
async def test_stale_persisted_exit_state_is_discarded_on_restore():
    """The invalidation half, asserted before the re-arm re-places anything.

    This is the assertion that fails on the unfixed code: `sl_placement_initiated`
    survives the restore as True, which both suppresses re-placement and
    disables the missing-SL watchdog.
    """
    controller, _, _ = _build_controller(_persisted_position())

    await controller._load_runtime_state()
    restored = controller._active_position_get(SYMBOL, "futures_usdtm")

    assert restored is not None, "the position itself must still be restored"
    assert restored.sl_placement_initiated is False
    assert restored.current_sl_order_id is None
    assert restored.current_sl_client_order_id is None
    assert restored.is_sl_algo_order is False
    assert restored.ptp_placement_initiated_flags == {}

    stale_tp = restored.partial_tp_orders[0]
    assert stale_tp.order_id is None
    assert stale_tp.client_order_id is None


@pytest.mark.asyncio
async def test_restored_partial_take_profit_is_re_placed_as_a_resting_limit_order():
    """Partial TPs are resting orders too, and they died the same way."""
    controller, paper_executor, _ = _build_controller(_persisted_position())

    position = await _restart(controller)

    limits = [o for o in paper_executor._open_orders.values() if o["type"] == "LIMIT"]
    assert len(limits) == 1, "the pending partial TP must be re-placed"
    assert limits[0]["side"] == "SELL"
    assert float(limits[0]["price"]) == pytest.approx(TAKE_PROFIT, rel=1e-9)

    tp = position.partial_tp_orders[0]
    assert tp.order_id is not None
    assert tp.client_order_id is not None
    assert tp.status == "PENDING"


@pytest.mark.asyncio
async def test_re_armed_stop_actually_fills_once_price_crosses():
    """The end-to-end consequence: the restored order is live, not decorative.

    Drives the real `PaperTradingExecutor.check_open_orders()` with the mark
    price below the stop. Only the settlement sink (`place_order`) is replaced,
    to keep the DB write out of a unit test; the trigger logic is production's.
    """
    controller, paper_executor, data_consumer = _build_controller(
        _persisted_position()
    )
    await _restart(controller)

    fills = []

    async def _recording_place_order(symbol, side, order_type, **kwargs):
        fills.append({"symbol": symbol, "side": side, "type": order_type, **kwargs})
        return {
            "orderId": 1,
            "clientOrderId": "fill-1",
            "avgFillPrice": PRICE_AFTER_STOP_CROSSED,
            "filledQty": kwargs.get("quantity"),
        }

    paper_executor.place_order = _recording_place_order

    # Before the cross, nothing fills.
    await paper_executor.check_open_orders()
    assert fills == [], "the stop must rest, not fill, while price is above it"

    # Price falls through the stop.
    data_consumer.price = PRICE_AFTER_STOP_CROSSED
    await paper_executor.check_open_orders()

    assert len(fills) == 1, f"the restored stop-loss never triggered: {fills}"
    assert fills[0]["type"] == "MARKET"
    assert fills[0]["side"] == "SELL"
    assert float(fills[0]["quantity"]) == pytest.approx(REMAINING_QTY, rel=1e-9)

    stop_ids = [
        cid
        for cid, o in paper_executor._open_orders.items()
        if o["type"] == "STOP_MARKET"
    ]
    assert not stop_ids, "a triggered stop must be removed from the order book"


@pytest.mark.asyncio
async def test_re_running_the_re_arm_places_no_duplicate_orders():
    """Idempotency: a second re-arm must not double up on the order book."""
    controller, paper_executor, _ = _build_controller(_persisted_position())

    await _restart(controller)
    first = dict(paper_executor._open_orders)

    await controller._rearm_paper_exit_orders_after_restart()
    second = dict(paper_executor._open_orders)

    assert len(first) == 2, f"expected SL + one partial TP, got {len(first)}"
    assert set(first) == set(second), (
        f"second re-arm changed the order book: {set(first)} -> {set(second)}"
    )


# --- The live path must be untouched -----------------------------------------


@pytest.mark.asyncio
async def test_live_controller_keeps_its_persisted_exit_order_state():
    """A live controller's orders are real and sit on the exchange.

    The exchange confirms the position, so the persisted SL id and
    `sl_placement_initiated` flag must survive the restore untouched -- this fix
    is paper-only and must not silently strip live protection.
    """
    live_position = _persisted_position(
        mode="live", api_key_id=42, current_sl_order_id="99887766"
    )
    live_executor = _FakeLiveExecutor(
        [{"symbol": SYMBOL, "positionAmt": str(REMAINING_QTY), "entryPrice": "85000"}]
    )
    controller, paper_executor, _ = _build_controller(live_position, live_executor)

    await controller._load_runtime_state()
    restored = controller._active_position_get(SYMBOL, "futures_usdtm")

    assert restored is not None
    assert restored.sl_placement_initiated is True
    assert restored.current_sl_order_id == "99887766"
    assert restored.current_sl_client_order_id == "x-sl-deadbeefdeadbeef"

    # And the re-arm declines to touch a live controller's order book.
    await controller._rearm_paper_exit_orders_after_restart()
    assert paper_executor._open_orders == {}
    assert restored.current_sl_order_id == "99887766"


@pytest.mark.asyncio
async def test_live_controller_without_executor_keeps_its_persisted_state():
    """The dangerous variant: a live controller whose executor failed to build.

    It has no live executor, but its orders are still real. Falling back to
    "no executor means paper" would strip a live stop-loss, so the discriminator
    is `api_key_id`, not executor presence.
    """
    live_position = _persisted_position(
        mode="live", api_key_id=42, current_sl_order_id="99887766"
    )
    controller, _, _ = _build_controller(live_position, live_executor=None)
    controller.api_key_id = 42
    controller.api_key_name = "live"

    await controller._load_runtime_state()
    restored = controller._active_position_get(SYMBOL, "futures_usdtm")

    assert restored is not None, "the position must still be restored"
    assert restored.sl_placement_initiated is True
    assert restored.current_sl_order_id == "99887766"

    await controller._rearm_paper_exit_orders_after_restart()
    assert restored.current_sl_order_id == "99887766"