"""The trading gate must name its cause, and an unverifiable start-up latch must heal.

THE DEFECT
----------
On 2026-10-19 production's `is_trading_allowed` was false from ~09:58Z to
~18:41Z and every signal was rejected with

    [ProcessSignal:...] Signal REJECTED by rm.is_symbol_trading_allowed (general block).

"general block" is not a cause. The gate returns a bare ``False`` for several
completely different reasons -- the emergency-stop latch, the global risk
limits, the symbol blacklist, a failed blacklist query -- and the operator was
told nothing except those two words.

The cause in that incident was the EMERGENCY-STOP LATCH, engaged by
``TradingController._restore_emergency_stop()``'s fail-closed branch because
Redis was momentarily unreachable at bot start. That branch is an
INFRASTRUCTURE failure, not the operator's decision, but it was latched with
the same semantics as the operator's kill switch: only an explicit
``clear_emergency_stop()`` or a process restart could release it. So a Redis
blip silently stopped all trading for a whole day.

These tests drive the REAL RiskManager and the REAL controller methods (with a
fake Redis), and assert OUTCOMES rather than the shape of the fix.
"""

import asyncio
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_module import controller as ctrl_module  # noqa: E402
from bot_module import risk_manager as rm_module  # noqa: E402
from bot_module.controller import TradingController  # noqa: E402
from bot_module.risk_manager import RiskManager  # noqa: E402
from bot_module.strategy import SignalDirection, StrategySignal  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the latch uses.

    A dict, so the store genuinely survives being handed to a SECOND
    controller -- that handover is what "restart" means here.
    """

    def __init__(self, store=None):
        self.store = {} if store is None else store
        self.get_calls = []
        self.set_calls = []
        self.delete_calls = []

    async def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key, value):
        self.set_calls.append((key, value))
        self.store[key] = value

    async def delete(self, key):
        self.delete_calls.append(key)
        self.store.pop(key, None)

    def key_for(self, user_id):
        return f"emergency_stop:{user_id}"


class ExplodingRedis(FakeRedis):
    """Redis whose READ fails -- the fail-closed branch at start-up."""

    def __init__(self, store=None):
        super().__init__(store)
        self.reads_raise = True

    async def get(self, key):
        self.get_calls.append(key)
        if self.reads_raise:
            raise ConnectionError("redis is down")
        return self.store.get(key)


class _FakeDbSession:
    """The blacklist query resets the SQLAlchemy session cache first."""

    def __init__(self):
        self.expire_calls = 0

    def expire_all(self):
        self.expire_calls += 1


class _RecordingTradeLogger:
    """Captures the trade-log events the controller emits."""

    def __init__(self):
        self.events = []

    def log_event(self, event_type=None, data=None):
        self.events.append((event_type, data))

    def start(self):
        pass

    def stop(self):
        pass


def _real_risk_manager():
    """A real RiskManager with its heavy collaborators stubbed out.

    Only the executor/paper-executor/db handles are None; every latch and gate
    code path under test is the genuine implementation.
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


# ---------------------------------------------------------------------------
# Part 1 -- the gate names its cause
# ---------------------------------------------------------------------------


async def test_emergency_stop_reason_is_reported_by_the_gate():
    """The latch's own reason text must reach the operator."""
    rm = _real_risk_manager()
    rm.trigger_emergency_stop("redis read failed at startup - cannot verify")

    allowed = await rm.is_symbol_trading_allowed("BTCUSDT")

    assert allowed is False
    reason = rm.get_trading_disabled_reason()
    assert reason is not None, (
        "the gate refused the symbol but recorded no reason -- the operator "
        "would see only 'general block'"
    )
    assert "EMERGENCY STOP" in reason
    assert "redis read failed at startup" in reason, (
        "the emergency stop's own reason text must be included, otherwise the "
        "operator still cannot tell what engaged the latch"
    )


async def test_risk_limit_reason_names_the_number_that_tripped():
    """A breached limit must be reported with the measurement and threshold."""
    rm = _real_risk_manager()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0
    rm.stats.today_pnl = -200.0  # -20% of the day's balance
    rm.stats.consecutive_losses = 0
    rm.max_drawdown_threshold = 0.10  # 10%
    rm.daily_max_loss_threshold = 0.99  # not the one that trips
    rm.min_balance_threshold = 5.0
    rm._is_trading_allowed = True

    rm._check_risk_limits()
    allowed = await rm.is_symbol_trading_allowed("BTCUSDT")

    assert allowed is False
    reason = rm.get_trading_disabled_reason()
    assert reason is not None
    assert "drawdown" in reason.lower()
    assert "20.00%" in reason, (
        f"the measured drawdown must be reported, got: {reason!r}"
    )
    assert "10.00%" in reason, (
        f"the threshold must be reported, got: {reason!r}"
    )


async def test_consecutive_loss_limit_reason_names_the_count(monkeypatch):
    """The consecutive-loss limit must be reported with its numbers too."""
    rm = _real_risk_manager()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0
    rm.stats.today_pnl = 0.0
    rm.stats.consecutive_losses = 10
    rm.max_drawdown_threshold = 0.99
    rm.daily_max_loss_threshold = 0.99
    rm.max_consecutive_losses = 10
    rm.min_balance_threshold = 5.0
    rm._is_trading_allowed = True

    rm._check_risk_limits()
    allowed = await rm.is_symbol_trading_allowed("BTCUSDT")

    assert allowed is False
    reason = rm.get_trading_disabled_reason()
    assert reason is not None
    assert "consec losses" in reason.lower()
    assert "10" in reason


async def test_blacklist_reason_names_the_symbol_and_the_until(monkeypatch):
    """A blacklisted symbol must say which symbol and until when."""

    class _Cfg:
        risk_management = {
            "blacklist": {
                "coins": [
                    {
                        "symbol": "BTCUSDT",
                        "until": "2099-01-01T00:00:00+00:00",
                        "reason": "operator blacklisted after a bad trade",
                    }
                ]
            }
        }

    class _Crud:
        async def get_config(self, _session, user_id=None):
            return _Cfg()

    monkeypatch.setattr(rm_module, "crud", _Crud())

    rm = _real_risk_manager()
    rm.db_session = _FakeDbSession()
    rm._is_trading_allowed = True

    allowed = await rm.is_symbol_trading_allowed("BTCUSDT")

    assert allowed is False
    reason = rm.get_trading_disabled_reason()
    assert reason is not None
    assert "BTCUSDT" in reason
    assert "BLACKLISTED" in reason
    assert "2099-01-01" in reason


async def test_gate_reports_the_reason_even_if_the_latch_was_set_directly():
    """The gate must derive the reason from the latch state ITSELF.

    The reason is recorded in two places: when the latch is engaged, and when
    the gate refuses. A latch that was engaged by any other route (a restored
    RiskManager, a test double) must still produce a reason, or the operator
    is back to "general block".
    """
    rm = _real_risk_manager()
    rm._emergency_stop_active = True
    rm._emergency_stop_reason = "redis unavailable at startup - cannot verify"

    allowed = await rm.is_symbol_trading_allowed("BTCUSDT")

    assert allowed is False
    reason = rm.get_trading_disabled_reason()
    assert reason is not None
    assert "EMERGENCY STOP" in reason
    assert "redis unavailable at startup" in reason


async def test_a_healthy_gate_records_no_block_reason():
    """Nothing blocked -> nothing to report."""
    rm = _real_risk_manager()
    rm._is_trading_allowed = True

    allowed = await rm.is_symbol_trading_allowed("BTCUSDT")

    assert allowed is True
    assert rm.get_trading_disabled_reason() is None


# ---------------------------------------------------------------------------
# Part 2 -- an unverifiable start-up latch self-heals; an operator's does not
# ---------------------------------------------------------------------------


async def test_operator_stop_still_blocks_and_survives_a_restart():
    """THE control. The operator's kill switch must not weaken.

    A stop written by the operator (the API's EMERGENCY_STOP endpoint, or
    _persist_emergency_stop) is mirrored into Redis and restored on the next
    start. It must keep requiring an explicit clear.
    """
    redis_store = FakeRedis()

    controller_a = _controller(_real_risk_manager(), redis_store)
    await controller_a._persist_emergency_stop("operator hit the kill switch")
    controller_a.rm.trigger_emergency_stop("operator hit the kill switch")

    # --- restart: new controller, new RiskManager, same Redis ---
    controller_b = _controller(_real_risk_manager(), redis_store)
    await controller_b._restore_emergency_stop()

    assert controller_b.rm.is_emergency_stop_active() is True, (
        "an operator's stop must survive a restart"
    )
    assert controller_b.rm._is_trading_allowed is False
    assert controller_b.rm.is_emergency_stop_operator_initiated() is True, (
        "a restored persisted stop is an OPERATOR stop and must keep "
        "requiring an explicit clear"
    )

    # ...and the self-heal must refuse to touch it.
    released = await controller_b._maybe_release_unverified_emergency_stop()
    assert released is False, (
        "the self-heal released an operator's kill switch -- the operator's "
        "decision was overridden without anyone clearing it"
    )
    assert controller_b.rm.is_emergency_stop_active() is True
    assert controller_b.rm._is_trading_allowed is False


async def test_operator_stop_is_not_released_even_when_the_key_is_absent():
    """The latch's own origin decides, not just the presence of the key.

    If the key were somehow removed by hand while the operator's latch is
    engaged, the latch must STILL hold: the operator has not cleared it.
    """
    redis_store = FakeRedis()
    controller = _controller(_real_risk_manager(), redis_store)
    controller.rm.trigger_emergency_stop("EMERGENCY_STOP command from user 1")

    released = await controller._maybe_release_unverified_emergency_stop()

    assert released is False
    assert controller.rm.is_emergency_stop_active() is True
    assert controller.rm._is_trading_allowed is False


async def test_startup_redis_failure_blocks_initially_then_self_heals():
    """THE fix.

    Redis was unreachable when the controller came up, so the latch could not
    be verified and the controller stayed stopped (fail-closed). Once Redis is
    reachable again and no emergency stop is recorded, the latch must be
    released AUTOMATICALLY -- no restart, no operator action.
    """
    redis_store = ExplodingRedis()
    controller = _controller(_real_risk_manager(), redis_store)

    # --- start-up with Redis down ---
    await controller._restore_emergency_stop()

    assert controller.rm.is_emergency_stop_active() is True, (
        "an unverifiable emergency-stop state must fail CLOSED"
    )
    assert controller.rm._is_trading_allowed is False
    assert controller.rm.is_emergency_stop_operator_initiated() is False, (
        "a start-up verification failure is an INFRASTRUCTURE failure, not an "
        "operator decision"
    )
    assert await controller.rm.is_symbol_trading_allowed("BTCUSDT") is False

    # --- Redis comes back, and no emergency stop is recorded ---
    redis_store.reads_raise = False
    released = await controller._maybe_release_unverified_emergency_stop()

    assert released is True, (
        "the latch did not self-heal once Redis was reachable again and the "
        "emergency-stop key was absent -- a Redis blip at start-up silently "
        "killed all trading until the next restart"
    )
    assert controller.rm.is_emergency_stop_active() is False
    assert controller.rm._is_trading_allowed is True
    assert await controller.rm.is_symbol_trading_allowed("BTCUSDT") is True


async def test_startup_redis_failure_that_persists_keeps_trading_blocked():
    """Fail-closed intent preserved: still-unreachable Redis means still stopped."""
    redis_store = ExplodingRedis()
    controller = _controller(_real_risk_manager(), redis_store)

    await controller._restore_emergency_stop()
    released = await controller._maybe_release_unverified_emergency_stop()

    assert released is False, (
        "the latch was released while Redis was STILL unreachable -- a "
        "verification failure must never become permission to trade"
    )
    assert controller.rm.is_emergency_stop_active() is True
    assert controller.rm._is_trading_allowed is False
    assert await controller.rm.is_symbol_trading_allowed("BTCUSDT") is False


async def test_self_heal_keeps_the_latch_when_an_operator_stop_appears_later():
    """An operator stop that lands after the infra latch must win.

    The API writes the authoritative key BEFORE it publishes the command, so a
    stop raised while the bot was recovering is visible in Redis even if the
    bot missed the pub/sub message.
    """
    redis_store = ExplodingRedis()
    controller = _controller(_real_risk_manager(), redis_store)
    await controller._restore_emergency_stop()

    # Redis recovers AND the operator has stopped the account.
    redis_store.reads_raise = False
    redis_store.store[redis_store.key_for(1)] = json.dumps(
        {"reason": "EMERGENCY_STOP command from user 1"}
    )

    released = await controller._maybe_release_unverified_emergency_stop()

    assert released is False
    assert controller.rm.is_emergency_stop_active() is True
    assert controller.rm._is_trading_allowed is False
    assert controller.rm.is_emergency_stop_operator_initiated() is True
    assert "EMERGENCY_STOP command from user 1" in controller.rm._emergency_stop_reason


async def test_startup_without_any_redis_client_blocks_and_self_heals():
    """The OTHER fail-closed branch: the controller has no Redis client at all.

    `_restore_emergency_stop()` has two fail-closed branches -- "no redis
    client" and "the redis read raised". Both are infrastructure failures and
    both must self-heal once Redis is available again. This covers the first
    one; `test_startup_redis_failure_blocks_initially_then_self_heals` covers
    the second.
    """
    rm = _real_risk_manager()
    controller = _controller(rm, None)

    await controller._restore_emergency_stop()

    assert controller.rm.is_emergency_stop_active() is True, (
        "with no way to verify the emergency-stop state the controller must "
        "stay STOPPED (fail-closed)"
    )
    assert controller.rm._is_trading_allowed is False
    assert controller.rm.is_emergency_stop_operator_initiated() is False, (
        "a missing Redis client is an INFRASTRUCTURE failure, not an operator "
        "decision"
    )
    assert await controller.rm.is_symbol_trading_allowed("BTCUSDT") is False

    # Still no Redis: the self-heal cannot verify anything, so it must not
    # release the latch.
    released = await controller._maybe_release_unverified_emergency_stop()
    assert released is False
    assert controller.rm.is_emergency_stop_active() is True
    assert controller.rm._is_trading_allowed is False

    # Redis comes back and no emergency stop is recorded.
    controller.redis_client = FakeRedis()
    released = await controller._maybe_release_unverified_emergency_stop()
    assert released is True
    assert controller.rm.is_emergency_stop_active() is False
    assert controller.rm._is_trading_allowed is True


async def test_risk_manager_release_refuses_an_operator_latch_on_its_own():
    """The RiskManager's own release path must refuse an operator's latch.

    The controller checks the latch's origin before it calls this, so this is
    the second, independent line of defence. It has to hold on its own.
    """
    rm = _real_risk_manager()
    rm.trigger_emergency_stop("EMERGENCY_STOP command from user 1")

    released = rm.release_unverified_emergency_stop()

    assert released is False, (
        "the RiskManager released an operator-initiated latch on its own -- "
        "the operator's decision must only be reversible by the operator"
    )
    assert rm.is_emergency_stop_active() is True
    assert rm._is_trading_allowed is False


async def test_self_heal_does_not_reopen_the_gate_past_a_tripped_risk_limit():
    """The self-heal releases the LATCH only -- never a risk limit.

    `clear_emergency_stop()` re-enables the global gate unconditionally. If
    the self-heal used it, a drawdown that was still in force would be
    silently overridden by a Redis recovery. That would be loosening a risk
    control, which this change must not do.
    """
    redis_store = ExplodingRedis()
    controller = _controller(_real_risk_manager(), redis_store)
    await controller._restore_emergency_stop()

    # A drawdown limit trips while the start-up latch is engaged.
    rm = controller.rm
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0
    rm.stats.today_pnl = -500.0  # -50% drawdown
    rm.stats.consecutive_losses = 0
    rm.max_drawdown_threshold = 0.10
    rm.daily_max_loss_threshold = 0.99
    rm.min_balance_threshold = 5.0
    rm._check_risk_limits()

    redis_store.reads_raise = False
    released = await controller._maybe_release_unverified_emergency_stop()

    assert released is True, "the latch itself must be released"
    assert rm.is_emergency_stop_active() is False, "the latch must be gone"
    assert rm._is_trading_allowed is False, (
        "the global gate was re-opened while a drawdown limit was still in "
        "force -- the self-heal must not loosen a risk control"
    )
    assert await rm.is_symbol_trading_allowed("BTCUSDT") is False
    reason = rm.get_trading_disabled_reason()
    assert reason is not None
    assert "drawdown" in reason.lower()


async def test_self_heal_is_a_noop_when_nothing_is_stopped():
    """The common case must cost nothing and change nothing."""
    redis_store = FakeRedis()
    controller = _controller(_real_risk_manager(), redis_store)

    released = await controller._maybe_release_unverified_emergency_stop()

    assert released is False
    assert controller.rm.is_emergency_stop_active() is False
    assert controller.rm._is_trading_allowed is True
    assert redis_store.get_calls == [], (
        "the self-heal must not touch Redis when there is no latch to heal -- "
        "otherwise every signal pays a Redis round trip for nothing"
    )


# ---------------------------------------------------------------------------
# Part 1 (continued) -- the rejection log line names the reason
# ---------------------------------------------------------------------------


def _build_process_signal_controller(rm, redis_client):
    """A controller with just enough state to reach the rejection branch.

    `_process_signal` is the real method; this only supplies the handful of
    attributes it touches before the risk gate.
    """
    controller = TradingController.__new__(TradingController)
    controller.user_id = 1
    controller.api_key_name = "paper"
    controller.api_key_id = None
    controller.rm = rm
    controller.redis_client = redis_client
    controller.trade_logger = _RecordingTradeLogger()
    controller._processing_signal_lock = asyncio.Lock()
    controller._processing_signal_for_symbol = set()
    controller._recent_signals = {}
    controller._signal_throttle_period = 0
    controller._last_position_close_time_per_symbol = {}
    controller._symbol_cooldown_duration = 0
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ctrl_module.ActivePositionMap()
    controller._symbol_locks = {}
    controller.loop = asyncio.get_event_loop()
    return controller


def _signal():
    return StrategySignal(
        strategy_name="VisualBuilderStrategy",
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        stop_loss=49500.0,
        take_profit=51000.0,
        trigger_price=50000.0,
        details={},
    )


async def test_rejection_log_line_contains_the_reason_and_still_says_general_block(
    caplog,
):
    """The operator-facing line must keep its old wording AND gain the cause.

    Existing log greps match on "(general block)", so that text must survive.
    This drives the REAL `_process_signal` and inspects the REAL log record.
    """
    rm = _real_risk_manager()
    rm.trigger_emergency_stop("redis read failed at startup - cannot verify")

    controller = _build_process_signal_controller(rm, FakeRedis())

    with caplog.at_level(logging.WARNING, logger="bot_module.controller"):
        await controller._process_signal(_signal(), {"symbol": "BTCUSDT"})

    messages = [r.getMessage() for r in caplog.records]
    rejected = [m for m in messages if "is_symbol_trading_allowed" in m]
    assert rejected, f"the rejection was never logged: {messages}"
    assert "Signal REJECTED by rm.is_symbol_trading_allowed (general block)." in (
        rejected[0]
    ), f"the original wording must survive so existing greps still match: {rejected[0]}"
    assert "redis read failed at startup" in rejected[0], (
        f"the reason must be appended to the log line: {rejected[0]}"
    )


async def test_rejection_trade_log_event_carries_the_reason():
    """The trade log's rejection reason must name the cause too."""
    rm = _real_risk_manager()
    rm.trigger_emergency_stop("redis read failed at startup - cannot verify")

    controller = _build_process_signal_controller(rm, FakeRedis())
    await controller._process_signal(_signal(), {"symbol": "BTCUSDT"})

    events = [
        (et, data)
        for et, data in controller.trade_logger.events
        if et == "SIGNAL_REJECTED_RISK_BLOCK"
    ]
    assert events, (
        f"no risk-block event was logged: {controller.trade_logger.events}"
    )
    _event_type, data = events[0]
    assert "general block" in data["reason"], (
        f"the existing wording must survive: {data['reason']!r}"
    )
    assert "redis read failed at startup" in data["reason"], (
        f"the reason must be in the trade log too: {data['reason']!r}"
    )


# ---------------------------------------------------------------------------
# Part 3 -- the read-only endpoint reports the latch
# ---------------------------------------------------------------------------


class _FakeApiRedis(FakeRedis):
    """A fake Redis with the extra methods the endpoint uses."""

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]


def _run_endpoint(redis_client, user_id=1):
    from api.routes.portfolio import get_emergency_stop_state

    class _User:
        def __init__(self, uid):
            self.id = uid
            self.username = "tester"

    return asyncio.run(
        get_emergency_stop_state(
            redis_client=redis_client, current_user=_User(user_id)
        )
    )


def test_endpoint_reports_an_operator_stop():
    """The operator must be able to SEE the stop, not guess at it."""
    from api.routes.portfolio import EMERGENCY_STOP_KEY

    redis_client = _FakeApiRedis()
    redis_client.store[EMERGENCY_STOP_KEY.format(user_id=1)] = json.dumps(
        {
            "reason": "EMERGENCY_STOP command from user 1",
            "engaged_at_utc": "2026-10-19T10:00:00+00:00",
        }
    )

    payload = _run_endpoint(redis_client)["data"]

    assert payload["stopped"] is True
    assert payload["reason"] == "EMERGENCY_STOP command from user 1"
    assert payload["source"] == "operator"
    assert payload["persisted"] is True


def test_endpoint_reports_the_live_startup_unverified_latch():
    """The latch that was never persisted must still be visible.

    A start-up fail-closed latch has NO Redis key -- that is the whole
    problem. It only exists in the controller's memory, which the controller
    publishes. Without that the endpoint would report "not stopped" during
    exactly the incident it exists to explain.
    """
    redis_client = _FakeApiRedis()
    redis_client.store["depthsight:state:portfolio:1:None"] = json.dumps(
        {
            "user_id": 1,
            "is_trading_allowed": False,
            "emergency_stop_active": True,
            "emergency_stop_reason": (
                "redis unavailable at startup - cannot verify emergency-stop state"
            ),
            "emergency_stop_source": "startup_unverified",
        }
    )

    payload = _run_endpoint(redis_client)["data"]

    assert payload["stopped"] is True
    assert "redis unavailable at startup" in payload["reason"]
    assert payload["source"] == "startup_unverified"
    assert payload["persisted"] is False


def test_endpoint_reports_not_stopped_when_clear():
    """The negative case: nothing engaged -> the endpoint says so."""
    redis_client = _FakeApiRedis()
    redis_client.store["depthsight:state:portfolio:1:None"] = json.dumps(
        {
            "user_id": 1,
            "is_trading_allowed": True,
            "emergency_stop_active": False,
            "emergency_stop_reason": None,
            "emergency_stop_source": None,
        }
    )

    payload = _run_endpoint(redis_client)["data"]

    assert payload["stopped"] is False
    assert payload["reason"] is None
    assert payload["source"] is None
    assert payload["persisted"] is False


def test_endpoint_does_not_mutate_anything():
    """It is a READ-ONLY endpoint. Nothing may be written or deleted."""
    from api.routes.portfolio import EMERGENCY_STOP_KEY

    redis_client = _FakeApiRedis()
    key = EMERGENCY_STOP_KEY.format(user_id=1)
    redis_client.store[key] = json.dumps({"reason": "stop"})
    before = dict(redis_client.store)

    _run_endpoint(redis_client)

    assert redis_client.store == before, "the endpoint mutated Redis"
    assert redis_client.set_calls == []
    assert redis_client.delete_calls == []


def test_controller_publishes_the_live_latch_for_the_endpoint_to_read():
    """The endpoint can only see the live latch if the bot publishes it."""
    rm = _real_risk_manager()
    rm.trigger_emergency_stop(
        "redis unavailable at startup - cannot verify emergency-stop state",
        operator_initiated=False,
    )
    controller = _controller(rm, FakeRedis())

    state = controller._emergency_stop_state_for_publish()

    assert state["emergency_stop_active"] is True
    assert state["emergency_stop_source"] == "startup_unverified"
    assert "redis unavailable at startup" in state["emergency_stop_reason"]


def test_controller_publishes_an_operator_latch_as_an_operator_latch():
    """The operator's stop must be distinguishable from the infra latch."""
    rm = _real_risk_manager()
    rm.trigger_emergency_stop("EMERGENCY_STOP command from user 1")

    state = _controller(rm, FakeRedis())._emergency_stop_state_for_publish()

    assert state["emergency_stop_active"] is True
    assert state["emergency_stop_source"] == "operator"
    assert state["emergency_stop_reason"] == "EMERGENCY_STOP command from user 1"


def test_controller_publishes_no_latch_when_nothing_is_stopped():
    """A clear controller must not report a stop."""
    rm = _real_risk_manager()

    state = _controller(rm, FakeRedis())._emergency_stop_state_for_publish()

    assert state["emergency_stop_active"] is False
    assert state["emergency_stop_reason"] is None
    assert state["emergency_stop_source"] is None
