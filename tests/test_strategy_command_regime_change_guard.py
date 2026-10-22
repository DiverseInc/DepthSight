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
its command, and the guard honours that mark.

The APPROVAL-TIME state of the option is read from the payload's
`approved_breakeven_on_regime_change` sibling -- NOT from `config_data`. The
rehydrate replaces `config_data` with the CURRENT Postgres copy and writes
that back over the cached payload, so anything read out of `config_data` there
is the current state, not the approved one. See
`test_the_approval_record_survives_the_refresh_and_the_write_back`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging

import pytest

from bot_module import controller as controller_module
from bot_module import config as bot_config
from bot_module.controller import TradingController


CONFIG_ID = "0123456789abcdef0123456789abcdef"


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


class _FakeRedis:
    """A Redis stand-in shared by the rehydrate publisher and the listener.

    It stores what the publisher writes back (so the write-back can be
    observed) and records what the publisher publishes (so the real command
    listener can consume the real command).
    """

    def __init__(self):
        self.store: dict = {}
        self.sets: dict = {}
        self.published: list = []
        # The controller whose listener is consuming this Redis. The fake
        # pubsub stops that listener's `while self._running:` loop.
        self.controller = None

    async def scan_iter(self, match=None, count=None):
        # Both publishers scan, for different patterns:
        #   controller._rehydrate_running_strategies_from_redis -> "running_strategies:*"
        #   bot_runner._rehydrate_all_strategies_globally       -> "running_strategy_payload:*"
        # Real SCAN matches on the keyspace, so the fake honours the glob over
        # the SET keys and the string keys rather than only over one of them.
        for key in list(self.sets) + list(self.store):
            if match and not fnmatch.fnmatch(key, match):
                continue
            yield key

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.store[key] = value

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def publish(self, channel, message):
        self.published.append((channel, message))

    def pubsub(self):
        return _FakePubSub(self)


class _FakePubSub:
    """Delivers the last published command, then stops the listener loop."""

    def __init__(self, redis):
        self._redis = redis
        self._delivered = False

    async def subscribe(self, channel):
        return None

    async def unsubscribe(self, channel):
        return None

    async def get_message(self, ignore_subscribe_messages=False, timeout=1.0):
        if self._delivered or not self._redis.published:
            return None
        self._delivered = True
        # Stop the `while self._running:` loop AFTER this message is handled.
        controller = self._redis.controller
        if controller is not None:
            controller._running = False
        _channel, message = self._redis.published[-1]
        return {"type": "message", "data": message}


def _controller(redis_client=None, get_db_session=None):
    """A minimal TradingController.

    Uses `__new__` + set-state-by-hand, the pattern already established in
    tests/test_safety_guard_regressions.py. Only the attributes the Redis
    command listener, the rehydrate publisher and
    `_handle_start_strategy_command` touch are set.
    """
    controller = TradingController.__new__(TradingController)
    controller.user_id = 1
    controller.api_key_id = None  # paper-only controller
    controller.api_key_name = "paper"
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {}
    controller._running = True
    controller.redis_client = redis_client
    controller.get_db_session = get_db_session
    return controller


def _start_command(
    *,
    arms_the_stop: bool,
    rehydrate: bool = False,
    approved_armed=None,
):
    """A `START_STRATEGY` command in the exact envelope the API publishes.

    `approved_armed` is the approval record: `True`/`False` when the API
    recorded it, `None` when the payload predates the record (the
    backward-compatible case).
    """
    config_data = {"strategy_name": "VisualBuilderStrategy"}
    if arms_the_stop:
        config_data["breakeven_on_regime_change"] = True
    payload = {
        "user_id": 1,
        "id": CONFIG_ID,
        "config_id": CONFIG_ID,
        "mode": "paper",
        "config_data": config_data,
    }
    if approved_armed is not None:
        payload["approved_breakeven_on_regime_change"] = approved_armed
    command = {"command": "START_STRATEGY", "payload": payload}
    if rehydrate:
        # Set by `_rehydrate_running_strategies_from_redis` for a replay of
        # an already-approved strategy.
        command["rehydrate"] = True
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
    redis_client = _FakeRedis()
    redis_client.controller = controller
    redis_client.published.append(
        ("depthsight:commands", json.dumps(command))
    )
    controller.redis_client = redis_client
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

    await _deliver(
        controller,
        _start_command(arms_the_stop=True, rehydrate=True, approved_armed=True),
    )

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
        _start_command(arms_the_stop=True, rehydrate=True, approved_armed=False),
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
        _start_command(arms_the_stop=False, rehydrate=True, approved_armed=False),
    )

    assert created_strategies == ["VisualBuilderStrategy"]


async def test_a_rehydrate_replay_without_an_approval_record_is_not_refused(
    created_strategies, monkeypatch
):
    """BACKWARD COMPATIBILITY: a payload with no approval record still starts.

    A strategy that was approved BEFORE the record shipped carries no
    `approved_breakeven_on_regime_change`. Refusing it would make a running
    strategy vanish on the first restart after the deploy -- a worse
    regression than the hole the record closes. The handler falls back to the
    payload's own arming and says so in the log.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    await _deliver(
        controller,
        _start_command(arms_the_stop=True, rehydrate=True, approved_armed=None),
    )

    assert created_strategies == ["VisualBuilderStrategy"], (
        "an already-approved armed strategy with no approval record must NOT "
        "be refused on the first restart after the deploy"
    )


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
                arms_the_stop=True, rehydrate=True, approved_armed=True
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
                arms_the_stop=True, rehydrate=True, approved_armed=True
            ),
        )

    assert created_strategies == ["VisualBuilderStrategy"]
    assert "breakeven_on_regime_change" not in caplog.text


# ---------------------------------------------------------------------------
# the approval record must survive the refresh and the write-back
# ---------------------------------------------------------------------------


class _FakeStrategyRow:
    """Just enough of a StrategyConfig row for the refresh to read."""

    def __init__(self, config_data):
        self.config_data = config_data


async def test_the_approval_record_survives_the_refresh_and_the_write_back(
    monkeypatch, created_strategies
):
    """THE DEFECT, REPRODUCED THE WAY IT ACTUALLY HAPPENS.

    The rehydrate publisher does three things in order: it READS the cached
    payload, it REFRESHES `config_data` from Postgres, and it WRITES the
    refreshed payload back over the cached key. Anything the approval signal
    was read out of `config_data` is therefore destroyed by the write-back:
    the next read sees the CURRENT config, not the approved one.

    The approval record is a SIBLING of `config_data` in the payload, so the
    refresh (which replaces `config_data` and nothing else) and the write-back
    (which writes that same dict back) both leave it alone.

    This test runs the REAL publisher twice against a real-ish Redis and a
    real Postgres row, then feeds the command it produced to the REAL command
    listener.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    redis_client = _FakeRedis()
    redis_client.sets[f"running_strategies:{1}"] = {CONFIG_ID}
    # The APPROVAL-TIME payload: the option was NOT armed when the API
    # approved this strategy, and the API recorded that.
    redis_client.store[f"running_strategy_payload:{1}:{CONFIG_ID}"] = json.dumps(
        {
            "user_id": 1,
            "id": CONFIG_ID,
            "config_id": CONFIG_ID,
            "mode": "paper",
            "config_data": {"strategy_name": "VisualBuilderStrategy"},
            "approved_breakeven_on_regime_change": False,
        }
    )

    # The user later PUTs the option on. The API's PUT /config route does not
    # apply the regime-change guard, so the row now carries an arming that
    # was never approved.
    async def _fake_get_db_session():
        yield object()

    async def _fake_get_strategy_config(db, user_id, config_id):
        return _FakeStrategyRow(
            {
                "strategy_name": "VisualBuilderStrategy",
                "breakeven_on_regime_change": True,
            }
        )

    monkeypatch.setattr(
        "bot_module.rehydrate.crud.get_strategy_config", _fake_get_strategy_config
    )

    publisher = _controller(
        redis_client=redis_client, get_db_session=_fake_get_db_session
    )

    # Pass 1: reads the approval-time payload, refreshes config_data, and
    # writes the refreshed payload back over the cached one.
    await publisher._rehydrate_running_strategies_from_redis()
    # Pass 2: reads what pass 1 left behind.
    await publisher._rehydrate_running_strategies_from_redis()

    assert len(redis_client.published) == 2, "the publisher must publish twice"
    _channel, raw = redis_client.published[-1]
    command = json.loads(raw)
    published_payload = command["payload"]

    # The refresh DID happen: config_data is now the armed Postgres copy.
    assert published_payload["config_data"].get("breakeven_on_regime_change") is True
    # But the APPROVAL RECORD survived it, and it still says the arming was
    # never approved.
    assert published_payload.get("approved_breakeven_on_regime_change") is False, (
        "the approval record must survive the refresh AND the write-back -- "
        "otherwise a never-approved arming is read back as approved on the "
        "next restart"
    )

    # And the command the publisher produced must not start an armed
    # strategy on a deployment that cannot deliver the regime data.
    listener = _controller(redis_client=redis_client)
    redis_client.controller = listener
    listener._running = True
    await listener._redis_command_listener()

    assert created_strategies == [], (
        "a never-approved arming must be REFUSED, not replayed with an inert "
        "stop -- the approval record says it was never approved"
    )


async def test_the_global_rehydrate_publisher_also_preserves_the_approval_record(
    monkeypatch, created_strategies
):
    """THE SECOND PUBLISHER. There are two, and only one was covered.

    `bot_runner._rehydrate_all_strategies_globally` is a SECOND, independent
    implementation of the same publish path: it scans
    `running_strategy_payload:*` (not `running_strategies:*`), parses the user
    id as a STRING rather than an int, and calls `refresh_payload_config_from_db`
    with a different db factory. It exists because the per-controller rehydrate
    is gated on the controller existing, so a controller that fails to init
    silently drops the user's strategies on every restart.

    A defect fix that covers only the controller publisher leaves this door
    open, and a bot restart that takes the global path would still replay a
    never-approved arming as though it had been approved. So this runs the real
    global publisher twice against a real refresh, the same two passes that
    expose an overwrite, and asserts the record survives both.
    """
    import bot_runner

    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    redis_client = _FakeRedis()
    redis_client.store[f"running_strategy_payload:{1}:{CONFIG_ID}"] = json.dumps(
        {
            "user_id": 1,
            "id": CONFIG_ID,
            "config_id": CONFIG_ID,
            "mode": "paper",
            "config_data": {"strategy_name": "VisualBuilderStrategy"},
            # Approved WITHOUT the stop armed.
            "approved_breakeven_on_regime_change": False,
        }
    )

    # The user later PUTs the option on through the unguarded
    # PUT /config/{config_id} route, so the row now carries an arming that was
    # never approved on this deployment.
    async def _fake_get_db():
        yield object()

    async def _fake_get_strategy_config(db, user_id, config_id):
        return _FakeStrategyRow(
            {
                "strategy_name": "VisualBuilderStrategy",
                "breakeven_on_regime_change": True,
            }
        )

    monkeypatch.setattr(
        "bot_module.rehydrate.crud.get_strategy_config", _fake_get_strategy_config
    )

    # Pass 1 refreshes config_data and writes the result back over the cached
    # key; pass 2 reads what pass 1 left behind.
    await bot_runner._rehydrate_all_strategies_globally(redis_client)
    await bot_runner._rehydrate_all_strategies_globally(redis_client)

    assert len(redis_client.published) == 2, (
        "the global publisher must publish once per pass"
    )
    command = json.loads(redis_client.published[-1][1])
    published_payload = command["payload"]

    assert published_payload["config_data"].get("breakeven_on_regime_change") is True, (
        "the refresh must have happened, or this test proves nothing"
    )
    assert published_payload.get("approved_breakeven_on_regime_change") is False, (
        "the GLOBAL rehydrate publisher must preserve the approval record "
        "through its refresh and its write-back, exactly as the per-controller "
        "one does. It is a separate implementation of the same path and it is "
        "the one that runs when a controller failed to init."
    )

    listener = _controller(redis_client=redis_client)
    redis_client.controller = listener
    listener._running = True
    await listener._redis_command_listener()

    assert created_strategies == [], (
        "a never-approved arming replayed by the global publisher must be "
        "REFUSED, not started with an inert stop"
    )


async def test_a_rehydrate_replay_without_an_approval_record_says_so_loudly(
    created_strategies, monkeypatch, caplog
):
    """The fallback must be VISIBLE, or it is a silent hole.

    An ABSENT record means the strategy was approved before the record shipped,
    so the handler falls back to the payload's own arming rather than refusing
    a running strategy. That fallback is the right call -- refusing would make
    a user's strategy vanish on the first restart after the deploy -- but it
    re-opens the hole this PR closes for exactly that payload, and silently.

    So the fallback has to say so, at WARNING, naming the option. An operator
    who sees it knows the strategy will keep running until it is re-approved.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    controller = _controller()

    with caplog.at_level(logging.WARNING, logger="bot_module.controller"):
        await _deliver(
            controller,
            _start_command(arms_the_stop=True, rehydrate=True, approved_armed=None),
        )

    assert created_strategies == ["VisualBuilderStrategy"]
    assert "breakeven_on_regime_change" in caplog.text, (
        "falling back to the payload's own arming must be logged at WARNING, "
        "naming the option -- otherwise the hole this PR closes is re-opened "
        "silently for every pre-record strategy"
    )
    assert "predates" in caplog.text or "no approval record" in caplog.text, (
        "the warning must say WHY it fell back, so an operator can tell a "
        "deliberate fallback from a guard that misfired"
    )
