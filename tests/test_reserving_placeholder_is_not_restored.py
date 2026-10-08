# File: tests/test_reserving_placeholder_is_not_restored.py
"""
A `RESERVING` position is an in-process mutex and must never outlive its process.

THE DEFECT
----------
`_process_signal` inserts a zero-quantity placeholder with status `RESERVING`
before doing any exchange work, so two signals for the same symbol cannot race
each other while the first places its entry order. The placeholder is popped
when that work finishes.

Two things made that transient lock permanent:

1. **`_save_runtime_state` persisted every position regardless of status.**
   The comprehension was `{k: v.to_dict() for k, v in _active_positions.items()}`
   with no filter, so a `RESERVING` placeholder was written to Redis.

2. **`_load_runtime_state` restored it.** A reservation is owned by a specific
   running process. Restoring one from a previous process means nothing will
   ever release it -- the process that owned it is gone.

The result is a symbol that can never open a position again, invisible to the
API, and quietly consuming a `max_concurrent_trades` slot, because `RESERVING`
is in that status tuple. `_publish_state_to_redis` skips `status != "OPEN"`, so
`/api/v1/positions` returns `[]` while the bot is still holding the symbol.

Observed live 2026-10-07: `BTCUSDT=RESERVING`, surviving a full container
restart, surfaced only by `_ensure_paper_exit_orders_present`'s "NOT in OPEN
status" warning on the dashboard's Critical Events panel.

The failure mode is specifically bad because the obvious remedy cannot work. A
leak that is purely in memory is cleared by a restart; this one was written to
Redis, so every restart restored it. Operators restart a bot to clear stale
state -- that instinct actively made this worse.

WHY THIS TEST SHAPE
-------------------
It drives the REAL `_load_runtime_state` against a real `PaperTradingExecutor`,
the same harness `test_paper_exit_orders_survive_restart.py` uses, and feeds it
a snapshot produced by the real `LivePosition.to_dict`. No production logic is
copied. The state under test is serialised the same way production serialises
it, so a round-trip through the real code is exercised rather than a hand-built
approximation.
"""

import asyncio

import pytest

from bot_module.controller import LivePosition, TradingController

from .test_paper_exit_orders_survive_restart import (
    SYMBOL,
    USER_ID,
    _build_controller,
)

MARKET_TYPE = "futures_usdtm"


def _persisted_reserving_position() -> LivePosition:
    """A placeholder exactly as `_process_signal` creates it (controller.py:7862).

    Built by taking the proven OPEN fixture and mutating it into the shape
    `_process_signal` reserves, rather than constructing `LivePosition` from
    scratch -- so required constructor fields cannot drift out of sync with the
    real dataclass again.
    """
    from .test_paper_exit_orders_survive_restart import _persisted_position

    position = _persisted_position(
        entry_price=0.0,
        initial_quantity=0.0,
        remaining_quantity=0.0,
        status="RESERVING",
        client_order_id="RESERVE_1759848636000",
    )
    return position


def _snapshot(position: LivePosition) -> dict:
    """Serialised through the REAL producer, `LivePosition.to_dict`."""
    return {
        "serialization_format": "json",
        "timestamp": 1759848636.0,
        "monitored_symbols": [SYMBOL],
        "closing_managed_symbols": [],
        "last_known_symbols": [SYMBOL],
        "currently_managed_symbols": [SYMBOL],
        "active_positions": {f"paper:{SYMBOL}": position.to_dict()},
    }


@pytest.mark.asyncio
async def test_reserving_placeholder_is_not_restored_from_redis():
    """The core regression: a RESERVING record must not come back on startup.

    Restoring it blocks the symbol forever, hides it from the dashboard, and
    eats a concurrent-trades slot -- with no endpoint showing why.
    """
    controller, _, _ = _build_controller(_persisted_reserving_position())

    # Sanity: the state really does arrive carrying a RESERVING record.
    snapshot = controller.redis_client._snapshot
    raw = snapshot["active_positions"]
    assert any(
        v.get("status") == "RESERVING" for v in raw.values()
    ), f"test setup is wrong: no RESERVING record in {raw}"

    await controller._load_runtime_state()

    restored = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert restored is None, (
        f"A RESERVING placeholder was restored from Redis. It belongs to a "
        f"process that no longer exists, so nothing will ever release it: "
        f"{SYMBOL} is now blocked from trading permanently, is invisible to "
        f"/api/v1/positions, and consumes a max_concurrent_trades slot. "
        f"Restored record: status={getattr(restored, 'status', None)!r}"
    )


@pytest.mark.asyncio
async def test_discarded_reservation_is_reported_loudly(caplog):
    """Silently dropping it would hide a real stuck symbol.

    An operator needs to know a record was discarded, otherwise the same
    symptom reappears from another cause with no trace.
    """
    controller, _, _ = _build_controller(_persisted_reserving_position())

    with caplog.at_level("WARNING"):
        await controller._load_runtime_state()

    warnings = [r for r in caplog.records if r.levelno >= 30]
    assert any("RESERVING" in r.getMessage() for r in warnings), (
        "Discarding a stuck reservation must be logged at WARNING or above. "
        f"Got: {[r.getMessage() for r in warnings]}"
    )


@pytest.mark.asyncio
async def test_resaving_state_does_not_persist_a_reservation():
    """Defence in depth: stop writing them in the first place.

    The load-side filter alone would leave the serializer capable of writing a
    RESERVING record again -- and any other consumer reading that Redis key
    would still see it.
    """
    controller, _, _ = _build_controller(_persisted_reserving_position())

    # `_save_runtime_state` reads this; the shared harness does not set it, so
    # without this the save raises AttributeError, is swallowed into an ERROR
    # log, and never writes -- which would make this test pass vacuously.
    controller.symbol_selection_config = None
    snapshot_before = controller.redis_client._snapshot

    # Restore the placeholder into memory, as a crashed process would have left.
    await controller._load_runtime_state()
    leaked = _persisted_reserving_position()
    async with controller._positions_dict_lock:
        controller._active_position_set(leaked)

    await controller._save_runtime_state()

    # Prove the save actually happened, so a swallowed exception cannot make
    # this test green for the wrong reason.
    assert controller.redis_client._snapshot is not snapshot_before, (
        "_save_runtime_state did not write anything -- it probably raised and "
        "logged. A no-op save would make the assertion below vacuous."
    )
    assert (
        controller.redis_client._snapshot["serialization_format"] == "json"
        and "active_positions" in controller.redis_client._snapshot
    ), f"unexpected snapshot shape: {controller.redis_client._snapshot}"

    written = controller.redis_client._snapshot["active_positions"]
    assert not any(
        v.get("status") == "RESERVING" for v in written.values()
    ), (
        f"_save_runtime_state persisted a RESERVING placeholder: {written}. "
        f"It must be filtered out, or the record comes back on every restart."
    )


@pytest.mark.asyncio
async def test_a_real_open_position_still_restores():
    """The filter must not become a blunt instrument.

    Only RESERVING is dropped. A real position with quantity and a genuine
    order id must still survive the restart -- that is the entire purpose of
    persisting runtime state.
    """
    from .test_paper_exit_orders_survive_restart import _persisted_position

    controller, _, _ = _build_controller(_persisted_position())

    await controller._load_runtime_state()

    restored = controller._active_position_get(SYMBOL, MARKET_TYPE)
    assert restored is not None, (
        "A real OPEN position failed to restore. Dropping RESERVING must not "
        "cost us genuine position recovery across restarts."
    )
    assert restored.status == "OPEN"
    assert (restored.remaining_quantity or 0.0) > 0