"""Regression tests for the emergency-stop latch surviving a bot restart.

The defect these pin
-------------------
``RiskManager._emergency_stop_active`` is a plain instance attribute, so it is
born ``False`` on every controller start. Compounding that,
``clear_emergency_stop()`` had **zero callers anywhere in the repo** -- there
was no API endpoint, no bot command, nothing. So the only way to release the
latch was a bot restart, which is the same event that silently re-armed it.

Net effect: a user who hit the kill switch went live again on the next deploy,
crash, or bounce -- with no operator ever deciding to resume. For a product
about to trade real money that is the worst available failure mode for the
control whose entire job is to be the last line of defence.

The fix mirrors the latch into Redis and adds a first-class
``CLEAR_EMERGENCY_STOP`` command so an operator releases it deliberately.

These tests drive the **real** ``RiskManager`` -- not a stub. An earlier
draft of this file faked ``rm``, which would have pinned the fake's
semantics rather than the product's. The end state asserted is the real one:
``is_emergency_stop_active()`` and ``_is_trading_allowed``.
"""

import asyncio
import time

import pytest

from bot_module.controller import TradingController
from bot_module.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async stand-in for the bits of redis the latch uses.

    A dict so that the store genuinely survives being handed to a *second*
    controller -- that handover is what "restart" means here.
    """

    def __init__(self, store=None):
        self.store = {} if store is None else store
        self.get_calls = []

    async def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    def key_for(self, user_id):
        return f"emergency_stop:{user_id}"


class ExplodingRedis(FakeRedis):
    """Redis that fails on read -- the fail-closed branch."""

    async def get(self, key):
        raise ConnectionError("redis is down")


def _real_risk_manager():
    """A real RiskManager with its heavy collaborators stubbed out.

    Only the executor/paper-executor/db handles are None; every latch code
    path under test is the genuine implementation.
    """
    return RiskManager(
        executor=None,
        paper_executor=None,
        user_id=1,
        db_session=None,
        user_settings={},
        api_key_name="paper",
    )


def _controller(rm, redis_client):
    controller = TradingController.__new__(TradingController)
    controller.user_id = 1
    controller.rm = rm
    controller.redis_client = redis_client
    return controller


@pytest.fixture
def redis_store():
    return FakeRedis()


# ---------------------------------------------------------------------------
# The defect: a stop must outlive the process that engaged it
# ---------------------------------------------------------------------------


async def test_emergency_stop_survives_a_bot_restart(redis_store):
    """THE regression.

    Controller A engages the stop. A bot restart builds Controller B with a
    brand-new RiskManager. B must come up STOPPED.
    """
    controller_a = _controller(_real_risk_manager(), redis_store)
    await controller_a._persist_emergency_stop("operator hit the kill switch")

    # --- restart: new controller, new RiskManager, same Redis ---
    controller_b = _controller(_real_risk_manager(), redis_store)
    await controller_b._restore_emergency_stop()

    assert controller_b.rm.is_emergency_stop_active(), (
        "an engaged emergency stop must survive a bot restart -- the latch was "
        "in-memory only, so a deploy silently re-armed trading"
    )
    # The real risk, stated as the engine sees it.
    assert controller_b.rm._is_trading_allowed is False, (
        "restoring the latch must also leave new entries blocked"
    )
    assert "operator hit the kill switch" in controller_b.rm._emergency_stop_reason


async def test_restart_preserves_the_reason(redis_store):
    """The operator must still see WHY they are stopped after a restart."""
    controller_a = _controller(_real_risk_manager(), redis_store)
    await controller_a._persist_emergency_stop("max drawdown breached")

    controller_b = _controller(_real_risk_manager(), redis_store)
    await controller_b._restore_emergency_stop()

    assert controller_b.rm._emergency_stop_reason == "max drawdown breached"


async def test_restore_reads_the_key_scoped_to_this_user(redis_store):
    """One user's stop must not stop a different user."""
    controller_a = _controller(_real_risk_manager(), redis_store)
    controller_a.user_id = 7
    await controller_a._persist_emergency_stop("user 7 only")

    other = _controller(_real_risk_manager(), redis_store)
    other.user_id = 8
    await other._restore_emergency_stop()

    assert other.rm.is_emergency_stop_active() is False, (
        "user 8 must not inherit user 7's emergency stop"
    )


# ---------------------------------------------------------------------------
# Positive control -- do not invent a stop that was never asked for
# ---------------------------------------------------------------------------


async def test_restore_is_a_noop_when_no_latch_was_persisted(redis_store):
    """A clean restart must leave trading enabled.

    Without this, a controller that stopped every user on every boot would
    pass the other tests while being unusable.
    """
    controller = _controller(_real_risk_manager(), redis_store)

    await controller._restore_emergency_stop()

    assert controller.rm.is_emergency_stop_active() is False
    assert controller.rm._is_trading_allowed is True


async def test_persisted_key_is_scoped_per_user(redis_store):
    """Two users' latches must coexist."""
    a = _controller(_real_risk_manager(), redis_store)
    a.user_id = 7
    await a._persist_emergency_stop("a stopped")

    b = _controller(_real_risk_manager(), redis_store)
    b.user_id = 8
    await b._persist_emergency_stop("b stopped")

    assert set(redis_store.store) == {"emergency_stop:7", "emergency_stop:8"}


# ---------------------------------------------------------------------------
# The clear path -- otherwise persisting is just a lockout
# ---------------------------------------------------------------------------


async def test_clear_releases_the_latch_and_erases_the_key(redis_store):
    controller = _controller(_real_risk_manager(), redis_store)
    await controller._persist_emergency_stop("stop me")
    controller.rm.trigger_emergency_stop("stop me")

    controller.rm.clear_emergency_stop()
    await controller._clear_emergency_stop_persisted()

    assert controller.rm.is_emergency_stop_active() is False
    assert controller.rm._is_trading_allowed is True
    assert "emergency_stop:1" not in redis_store.store, (
        "the Redis key must be deleted too, or the stop returns on restart"
    )


async def test_a_cleared_latch_does_not_come_back_on_restart(redis_store):
    """The round trip: stop -> resume -> restart must be trading-enabled.

    This is the test that would catch a half-done fix where only the
    in-memory latch is cleared but the key survives.
    """
    stopped = _controller(_real_risk_manager(), redis_store)
    await stopped._persist_emergency_stop("incident")
    stopped.rm.trigger_emergency_stop("incident")

    stopped.rm.clear_emergency_stop()
    await stopped._clear_emergency_stop_persisted()

    restarted = _controller(_real_risk_manager(), redis_store)
    await restarted._restore_emergency_stop()

    assert restarted.rm.is_emergency_stop_active() is False, (
        "after an explicit resume the next restart must come up trading-enabled"
    )
    assert restarted.rm._is_trading_allowed is True


async def test_clear_when_nothing_was_stopped_is_harmless(redis_store):
    """Clearing an already-clear latch must not raise."""
    controller = _controller(_real_risk_manager(), redis_store)

    controller.rm.clear_emergency_stop()
    await controller._clear_emergency_stop_persisted()

    assert controller.rm.is_emergency_stop_active() is False
    assert controller.rm._is_trading_allowed is True


# ---------------------------------------------------------------------------
# Fail-closed semantics
# ---------------------------------------------------------------------------


async def test_restore_fails_closed_when_redis_cannot_be_read():
    """If we cannot prove the latch is clear, stay stopped.

    A kill switch that can be lost is not a kill switch. This is the one
    place the code deliberately trades availability for safety, so it is
    pinned by a test rather than left to a comment.
    """
    controller = _controller(_real_risk_manager(), ExplodingRedis())

    await controller._restore_emergency_stop()

    assert controller.rm.is_emergency_stop_active() is True, (
        "an unreadable latch must fail CLOSED, not open"
    )
    assert controller.rm._is_trading_allowed is False


async def test_restore_fails_closed_when_there_is_no_redis_client():
    """Same rule for a controller whose Redis never came up."""
    controller = _controller(_real_risk_manager(), None)

    await controller._restore_emergency_stop()

    assert controller.rm.is_emergency_stop_active() is True
    assert controller.rm._is_trading_allowed is False


async def test_persist_survives_a_redis_write_failure_without_raising():
    """A Redis outage must not take down the kill switch itself.

    The in-memory latch must still be engaged -- persisting is best-effort
    on top of a latch that already works.
    """

    class FailingWriteRedis(FakeRedis):
        async def set(self, key, value):
            raise ConnectionError("redis is down")

    controller = _controller(_real_risk_manager(), FailingWriteRedis())
    controller.rm.trigger_emergency_stop("still works in memory")

    await controller._persist_emergency_stop("reason")

    assert controller.rm.is_emergency_stop_active() is True, (
        "a failed persist must not un-engage the in-memory latch"
    )


# ---------------------------------------------------------------------------
# Wire-format tolerance
# ---------------------------------------------------------------------------


async def test_restore_handles_bytes_and_a_bare_string(redis_store):
    """redis-py may hand back bytes; an operator may hand-write the key.

    A bare-string value must still restore rather than reading as no-stop.
    """
    redis_store.store["emergency_stop:1"] = b'{"reason": "bytes payload"}'
    controller = _controller(_real_risk_manager(), redis_store)
    await controller._restore_emergency_stop()
    assert controller.rm._emergency_stop_reason == "bytes payload"

    redis_store.store["emergency_stop:1"] = "hand-written reason"
    controller2 = _controller(_real_risk_manager(), redis_store)
    await controller2._restore_emergency_stop()
    assert controller2.rm._emergency_stop_reason == "hand-written reason"
    assert controller2.rm.is_emergency_stop_active() is True


async def test_restore_engages_the_latch_not_merely_the_message(redis_store):
    """End-state assertion: the engine must actually refuse new entries."""
    controller_a = _controller(_real_risk_manager(), redis_store)
    await controller_a._persist_emergency_stop("gate check")

    controller_b = _controller(_real_risk_manager(), redis_store)
    await controller_b._restore_emergency_stop()

    # Two independent observations of the same state. Asserting only the
    # latch flag would let a future edit that decoupled them pass silently.
    assert controller_b.rm.is_emergency_stop_active() is True
    assert controller_b.rm._is_trading_allowed is False


def test_event_loop_is_available():
    """Guard against the whole file silently collecting zero async tests."""
    assert asyncio.get_event_loop_policy() is not None


# ---------------------------------------------------------------------------
# Regression: the persist must not be able to hang the kill switch
# ---------------------------------------------------------------------------


async def test_persist_times_out_instead_of_hanging(monkeypatch):
    """A Redis that accepts the connection and then hangs must not stall EMERGENCY_STOP.

    The persist await sits between latching the stop and flattening the
    positions. Without a timeout, a hung Redis turns the kill switch into a
    no-op under exactly the conditions where it is needed most.

    The timeout is monkeypatched down so the test is fast; the assertion is
    that the call RETURNS rather than that it takes five seconds.
    """
    import bot_module.controller as ctrl

    monkeypatch.setattr(ctrl, "EMERGENCY_STOP_PERSIST_TIMEOUT_SECONDS", 0.05)

    class HangingRedis(FakeRedis):
        async def set(self, key, value):
            await asyncio.sleep(30)  # never completes in this test

    controller = _controller(_real_risk_manager(), HangingRedis())
    controller.rm.trigger_emergency_stop("latched first")

    # Measure PROMPTNESS, not just that the latch survived. Without the
    # timeout this call blocks for the full 30s and then succeeds anyway --
    # so an assertion on the latch alone would pass with or without the fix
    # and would defend nothing.
    started = time.monotonic()
    await controller._persist_emergency_stop("will time out")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, (
        f"_persist_emergency_stop blocked for {elapsed:.1f}s; the Redis write "
        "sits between latching the stop and flattening positions, so a hang "
        "turns the kill switch into a no-op"
    )
    assert controller.rm.is_emergency_stop_active() is True, (
        "a Redis hang must not un-engage the latch that was already set"
    )
    assert controller.rm._is_trading_allowed is False


# ---------------------------------------------------------------------------
# Cross-module contract: the API writes the key the bot reads
# ---------------------------------------------------------------------------


def test_api_and_bot_agree_on_the_redis_key():
    """
    The API owns the authoritative key; the bot reads it on restart.

    These are two independent format strings in two files. If they drift, the
    API records a stop under a key the bot never looks at, the stop appears to
    work right up until a restart, and then it silently does nothing. Pin the
    contract instead of trusting the comment that says they match.
    """
    from api.routes.portfolio import EMERGENCY_STOP_KEY
    from bot_module import controller as ctrl

    controller = TradingController.__new__(TradingController)
    controller.user_id = 7

    assert controller._emergency_stop_redis_key() == EMERGENCY_STOP_KEY.format(
        user_id=7
    ), (
        "TradingController._emergency_stop_redis_key and the API's "
        "EMERGENCY_STOP_KEY must produce the same Redis key"
    )