"""The Redis-command path must not ARM a regime-change stop it cannot feed.

WHY
---
PR #28 closed the HTTP route: `POST /api/v1/strategies` answers **503** when a
launch would arm `breakeven_on_regime_change` on a deployment whose screener
WebSocket feed cannot deliver the regime data. The predicate lives in
`bot_module/regime_feed.py` (`screener_feed_can_deliver_regime_data`) and is
shared by both start paths.

But the API is not the only way a strategy gets started. The bot ALSO listens
for `START_STRATEGY` on its Redis command channel
(`TradingController._redis_command_listener`), and that path was unguarded: it
could still start a strategy with the stop armed on a deployment where no
regime data can ever arrive. The 503 protects the HTTP route only.

THE FAILURE MODE
----------------
`breakeven_on_regime_change` is implemented in `check_on_screener_update`
(bot_module/strategy.py:3070-3079). Its only production caller is
`_dynamic_symbol_selection_loop` in bot_module/controller.py, and that loop is
fed by the controller's screener-update queue. The only producer of that queue
is the `active_pairs_update` message on the main_app WebSocket
(bot_module/data_consumer.py:3428).

So on a deployment with no such feed the stop is inert from the first second:
the strategy starts, reports "running", and the user believes they are
protected. That is the same "armed but inert" hole the 503 closes -- through
the other door.

WHAT THESE TESTS DO
-------------------
They drive the REAL Redis command listener with a real `START_STRATEGY`
envelope -- the same `{"command": ..., "payload": {...}}` shape the API
publishes -- so this is the Redis-command path, not the HTTP endpoint.

Each case has a positive control. A guard that refused everything would pass
the refusal tests, so the "the feed is configured" case asserts that the
strategy is actually CREATED, not merely "not refused".

REHYDRATE
---------
A rehydrate replay is a strategy that was ALREADY approved by the API's 503
guard and was running before the bot restarted. Refusing to replay it would
make the user's running strategy vanish after a restart -- a different and
worse regression than an inert stop. The rehydrate publisher therefore marks
its command, and the guard honours that mark. See
`_rehydrate_running_strategies_from_redis` in bot_module/controller.py.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from bot_module import controller as controller_module
from bot_module import config as bot_config
from bot_module.controller import TradingController


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def feed_configured(monkeypatch):
    """The deployment that CAN deliver the regime data.

    Mirrors PR #28's `feed_configured` fixture in
    tests/test_regime_change_arm_guard.py, so the two paths are held to the
    same deployment definition.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "MAIN_APP")
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", True)
    monkeypatch.setattr(
        bot_config, "MAIN_APP_WS_URL", "wss://screener.example.test/ws/bot/"
    )


class _FakePubSub:
    """Delivers exactly one message, then stops the listener loop."""

    def __init__(self, controller, message):
        self._controller = controller
        self._message = message
        self._delivered = False
        self.subscribed_channel = None
        self.unsubscribed_channel = None

    async def subscribe(self, channel):
        self.subscribed_channel = channel

    async def unsubscribe(self, channel):
        self.unsubscribed_channel = channel

    async def get_message(self, ignore_subscribe_messages=False, timeout=1.0):
        if self._delivered:
            return None
        self._delivered = True
        # Stop the `while self._running:` loop AFTER this message is handled.
        self._controller._running = False
        return self._message


class _FakeRedis:
    """The only part of the redis client the command listener touches."""

    def __init__(self, controller, message):
        self._pubsub = _FakePubSub(controller, message)

    def pubsub(self):
        return self._pubsub


def _controller():
    """A minimal TradingController.

    Uses `__new__` + set-state-by-hand, the pattern already established in
    tests/test_safety_guard_regressions.py. Only the attributes the Redis
    command listener and `_handle_start_strategy_command` touch are set.
    """
    controller = TradingController.__new__(TradingController)
    controller.user_id = 1
    controller.api_key_id = None  # paper-only controller
    controller.api_key_name = "paper"
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {}
    controller._running = True
    return controller


def _start_command(
    *,
    arms_the_stop: bool,
    rehydrate: bool = False,
    armed_at_approval: bool = False,
):
    """A `START_STRATEGY` command in the exact envelope the API publishes."""
    config_data = {"strategy_name": "VisualBuilderStrategy"}
    if arms_the_stop:
        config_data["breakeven_on_regime_change"] = True
    payload = {
        "user_id": 1,
        "id": "0123456789abcdef0123456789abcdef",
        "config_id": "0123456789abcdef0123456789abcdef",
        "mode": "paper",
        "config_data": config_data,
    }
    command = {"command": "START_STRATEGY", "payload": payload}
    if rehydrate:
        # Set by `_rehydrate_running_strategies_from_redis` for a replay of
        # an already-approved strategy.
        command["rehydrate"] = True
        # Whether the option was armed in the APPROVAL-TIME payload.
        command["rehydrate_armed_at_approval"] = armed_at_approval
    return command


@pytest.fixture
def created_strategies(monkeypatch):
    """Record calls to `create_strategy_instance`.

    The spy returns None so the handler logs and returns instead of walking
    into real strategy construction. Whether the spy fired is the observable
    that separates "the guard let the command through" from "the handler
    refused it". Same pattern as tests/test_safety_guard_regressions.py.
    """
    calls = []

    def _spy(strategy_name, params=None):
        calls.append(strategy_name)
        return None

    monkeypatch.setattr(controller_module, "create_strategy_instance", _spy)
    return calls


async def _deliver(controller, command):
    """Run the real Redis command listener until it has handled `command`."""
    controller.redis_client = _FakeRedis(
        controller,
        {"type": "message", "data": json.dumps(command, default=str)},
    )
    controller._running = True
    await controller._redis_command_listener()


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------


async def test_the_redis_command_path_refuses_to_arm_the_stop_without_a_feed(
    created_strategies, monkeypatch
):
    """THE CASE: no screener feed, so the stop can never fire. Refuse.

    Before the fix the strategy was created, reported "running", and its
    regime-change stop was inert from the first second -- the same hole the
    HTTP 503 closes, reached through the other door.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    await _deliver(controller, _start_command(arms_the_stop=True))

    assert created_strategies == [], (
        "the Redis-command path must refuse to start a strategy that arms "
        "breakeven_on_regime_change when the deployment cannot deliver the "
        "regime data -- the strategy would start and its stop would never fire"
    )
    assert controller.running_strategy_instances == {}


async def test_the_refusal_is_visible_in_the_log(created_strategies, monkeypatch, caplog):
    """A refusal that only exists in a return statement is not an operation.

    The user needs to know WHY their strategy did not start, and an operator
    needs to know that the guard fired rather than some other early return.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    with caplog.at_level(logging.ERROR, logger="bot_module.controller"):
        await _deliver(controller, _start_command(arms_the_stop=True))

    assert "breakeven_on_regime_change" in caplog.text
    assert "STATIC_LIST" in caplog.text or "MAIN_APP_WS" in caplog.text


async def test_a_disabled_ws_feed_also_refuses_on_the_redis_path(
    created_strategies, feed_configured, monkeypatch
):
    """MAIN_APP_WS_ENABLED=false removes the only producer of the regime data."""
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", False)
    controller = _controller()

    await _deliver(controller, _start_command(arms_the_stop=True))

    assert created_strategies == []


async def test_an_unusable_ws_url_also_refuses_on_the_redis_path(
    created_strategies, feed_configured, monkeypatch
):
    """A URL that is not ws:// or wss:// cannot carry the feed."""
    monkeypatch.setattr(
        bot_config, "MAIN_APP_WS_URL", "https://screener.example.test/ws/bot/"
    )
    controller = _controller()

    await _deliver(controller, _start_command(arms_the_stop=True))

    assert created_strategies == []


# ---------------------------------------------------------------------------
# no false alarms
# ---------------------------------------------------------------------------


async def test_a_working_feed_still_starts_the_strategy_on_the_redis_path(
    created_strategies, feed_configured
):
    """POSITIVE CONTROL: a deployment that CAN deliver the data keeps it.

    Asserts that the strategy was CREATED, not merely "not refused": a
    handler that refused everything would satisfy "!= refused" while breaking
    every deployment that works.
    """
    controller = _controller()

    await _deliver(controller, _start_command(arms_the_stop=True))

    assert created_strategies == ["VisualBuilderStrategy"], (
        "a deployment whose screener feed is configured CAN deliver the regime "
        "data, so the strategy must still start on the Redis-command path"
    )


async def test_a_strategy_that_does_not_arm_the_stop_is_never_refused(
    created_strategies, monkeypatch
):
    """The guard must only fire for strategies that arm the option."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    await _deliver(controller, _start_command(arms_the_stop=False))

    assert created_strategies == ["VisualBuilderStrategy"]


async def test_the_option_explicitly_set_to_false_is_not_refused(
    created_strategies, monkeypatch
):
    """`False` is not `armed`. It must not be refused."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()
    command = _start_command(arms_the_stop=False)
    command["payload"]["config_data"]["breakeven_on_regime_change"] = False

    await _deliver(controller, command)

    assert created_strategies == ["VisualBuilderStrategy"]


# ---------------------------------------------------------------------------
# rehydrate
# ---------------------------------------------------------------------------


async def test_a_rehydrate_replay_of_an_approved_strategy_still_starts(
    created_strategies, monkeypatch
):
    """A rehydrate replay is NOT a new start. It must not be refused.

    The replayed strategy was already approved by the API's 503 guard and was
    running before the bot restarted. Refusing it would make the user's
    running strategy vanish after a restart -- a worse regression than an
    inert stop. The alarm added in PR #28 (the CRITICAL raised from the
    disabled-feed branch of `DataConsumer.start`) is what covers this case.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    await _deliver(controller, _start_command(arms_the_stop=True, rehydrate=True, armed_at_approval=True))

    assert created_strategies == ["VisualBuilderStrategy"], (
        "a rehydrate replay of an already-approved strategy must still start"
    )


async def test_a_rehydrate_replay_armed_after_approval_is_refused(
    created_strategies, monkeypatch
):
    """An arming that appeared AFTER the approval does NOT inherit the exemption.

    The API's `PUT /config/{config_id}` route writes config_data without
    applying the regime-change guard, and the replay refreshes config_data
    from Postgres. So the CURRENT config can carry an arming that was never
    approved on this deployment. That is a NEW arming and is refused like any
    other -- the exemption means "already approved", not "published by the
    rehydrate".
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    await _deliver(
        controller,
        _start_command(arms_the_stop=True, rehydrate=True, armed_at_approval=False),
    )

    assert created_strategies == [], (
        "an arming that was never approved must not inherit the rehydrate "
        "exemption -- it would start with an inert stop"
    )


async def test_a_rehydrate_replay_of_a_strategy_that_was_never_armed_still_starts(
    created_strategies, monkeypatch
):
    """A replay of a strategy that never armed the option is not affected."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    await _deliver(
        controller,
        _start_command(arms_the_stop=False, rehydrate=True, armed_at_approval=False),
    )

    assert created_strategies == ["VisualBuilderStrategy"]


async def test_a_rehydrate_replay_on_a_dead_feed_raises_the_alarm(
    created_strategies, monkeypatch, caplog
):
    """The replay is exempt from the REFUSAL, but it must not be SILENT.

    PR #28's CRITICAL alarm in `DataConsumer.start()` covers the replayed
    strategy only on a BEST-EFFORT basis: the rehydrate PUBLISHES the command
    to Redis and returns (the command-listener path routes by
    payload.user_id), so the listener starts the strategy asynchronously and
    the alarm may be evaluated before the replayed strategy is registered.
    So the alarm is emitted where the decision is made -- at the exact moment
    the replayed strategy starts.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(
            controller,
            _start_command(
                arms_the_stop=True, rehydrate=True, armed_at_approval=True
            ),
        )

    assert created_strategies == ["VisualBuilderStrategy"]
    assert "breakeven_on_regime_change" in caplog.text, (
        "a replayed strategy whose regime-change stop is inert must be LOUD -- "
        "it is exempt from the refusal, so nothing else would say so"
    )


async def test_a_rehydrate_replay_with_a_working_feed_raises_no_alarm(
    created_strategies, feed_configured, caplog
):
    """The alarm must NOT fire when the feed IS configured.

    The false-positive control for the alarm: it is a warning about an inert
    stop, and there is no inert stop when the feed works.
    """
    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(
            controller,
            _start_command(
                arms_the_stop=True, rehydrate=True, armed_at_approval=True
            ),
        )

    assert created_strategies == ["VisualBuilderStrategy"]
    assert "breakeven_on_regime_change" not in caplog.text
