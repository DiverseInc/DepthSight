"""
Regression tests: a bot restart must not replay a stale cached `config_data`.

Live incident 2026-10-10. A `blocks` -> `entryConditions` conversion was applied
to two paper strategies and confirmed `status: "running"`, `status_detail: null`
through the authenticated API. A bot restart roughly 90 seconds later
republished the pre-conversion snapshot and both strategies were back to
`cannot_trade` on the legacy format. The Postgres rows were correct the whole
time -- only the Redis snapshot was stale.

Both rehydrate layers replayed `running_strategy_payload:*` verbatim, and the
API DELETE+POST remediation was not durable: the next restart reverted it again.

These tests drive the REAL `TradingController._rehydrate_running_strategies_from_redis`
so that reverting the fix turns them red. Asserting on the helper in isolation
would not: it would prove the helper works, not that rehydrate calls it.
"""

import fnmatch
import json
import logging

import pytest

from bot_module import rehydrate as rehydrate_module
from bot_module.rehydrate import refresh_payload_config_from_db

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------

#: The pre-conversion body: the engine cannot read this format at all.
LEGACY_BLOCKS_CONFIG = {
    "symbol": "ETHUSDT",
    "timeframe": "15m",
    "blocks": [
        {
            "indicator": "BB",
            "period": 20,
            "std_dev": 2.0,
            "condition": "touches_lower",
            "action": "open_long",
        }
    ],
}

#: The converted body that actually lives in Postgres.
CONVERTED_CONFIG = {
    "symbol": "ETHUSDT",
    "timeframe": "15m",
    "entryConditions": [
        {"type": "bollinger_condition", "check_type": "price_below_lower"}
    ],
    "positionManagement": [
        {
            "type": "conditional_management",
            "then_actions": [{"action": "close_position"}],
        }
    ],
}

CONFIG_ID = "e43d1cb2-123c-4ca7-ba4f-bb25d245e969"
USER_ID = 1


class _Row:
    def __init__(self, config_data):
        self.config_data = config_data


class _FakeCrud:
    """Stands in for the configured `crud` runtime dependency."""

    def __init__(self):
        self.rows = {}
        self.raise_on_call = False
        self.calls = []

    async def get_strategy_config(self, db, user_id, config_id):
        self.calls.append((user_id, config_id))
        if self.raise_on_call:
            raise RuntimeError("simulated database outage")
        return self.rows.get((user_id, config_id))


@pytest.fixture
def fake_crud(monkeypatch):
    crud = _FakeCrud()
    monkeypatch.setattr(rehydrate_module, "crud", crud)
    return crud


def _db_factory():
    async def _factory():
        yield object()

    return _factory


def _payload(config_data, config_id=CONFIG_ID, user_id=USER_ID):
    return {
        "user_id": user_id,
        "id": config_id,
        "config_id": config_id,
        "mode": "paper",
        "symbol_selection_mode": "STATIC",
        "symbols": None,
        "config_data": config_data,
        "name": "paper-bollinger-1",
    }


class _FakeRedis:
    """Enough Redis surface for the rehydrate loop."""

    def __init__(self, store):
        self.store = dict(store)
        self.published = []

    async def scan_iter(self, match=None, count=None):
        for key in list(self.store):
            if match is None or fnmatch.fnmatch(key, match):
                yield key.encode()

    async def smembers(self, key):
        return {cid.encode() for cid in self.store[key]}

    async def get(self, key):
        value = self.store.get(key)
        return value.encode() if value is not None else None

    async def set(self, key, value):
        self.store[key] = value

    async def publish(self, channel, message):
        self.published.append((channel, message))


class _StubController:
    """Duck-typed `self` for the real unbound rehydrate method."""

    def __init__(self, redis_client, db_factory):
        self.redis_client = redis_client
        self.get_db_session = db_factory


# ---------------------------------------------------------------------------
# The seam: the real rehydrate loop
# ---------------------------------------------------------------------------


async def test_rehydrate_republishes_postgres_config_not_the_stale_cache(
    fake_crud,
):
    """
    THE regression test.

    The Redis snapshot holds the legacy `blocks` body while Postgres holds the
    converted one. The republished START_STRATEGY must carry the converted body.
    """
    from bot_module.controller import TradingController

    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)

    redis_client = _FakeRedis(
        {
            "running_strategies:1": {CONFIG_ID},
            f"running_strategy_payload:{USER_ID}:{CONFIG_ID}": json.dumps(
                _payload(LEGACY_BLOCKS_CONFIG)
            ),
        }
    )

    await TradingController._rehydrate_running_strategies_from_redis(
        _StubController(redis_client, _db_factory())
    )

    assert len(redis_client.published) == 1, "rehydrate must publish one command"
    _, raw_message = redis_client.published[0]
    command = json.loads(raw_message)

    assert command["command"] == "START_STRATEGY"
    published_config = command["payload"]["config_data"]

    assert "blocks" not in published_config, (
        "rehydrate replayed the legacy blocks config that the engine cannot read"
    )
    assert published_config == CONVERTED_CONFIG
    # Launch-time decisions must survive the refresh untouched.
    assert command["payload"]["symbols"] is None
    assert command["payload"]["symbol_selection_mode"] == "STATIC"


async def test_rehydrate_writes_the_refreshed_payload_back_to_redis(fake_crud):
    """
    Without the write-back, the global layer and the next restart would replay
    the same stale snapshot.
    """
    from bot_module.controller import TradingController

    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)

    cache_key = f"running_strategy_payload:{USER_ID}:{CONFIG_ID}"
    redis_client = _FakeRedis(
        {
            "running_strategies:1": {CONFIG_ID},
            cache_key: json.dumps(_payload(LEGACY_BLOCKS_CONFIG)),
        }
    )

    await TradingController._rehydrate_running_strategies_from_redis(
        _StubController(redis_client, _db_factory())
    )

    cached = json.loads(redis_client.store[cache_key])
    assert cached["config_data"] == CONVERTED_CONFIG


# ---------------------------------------------------------------------------
# Helper contract
# ---------------------------------------------------------------------------


async def test_stale_cache_is_replaced_by_postgres_config(fake_crud):
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)

    payload, refreshed = await refresh_payload_config_from_db(
        _payload(LEGACY_BLOCKS_CONFIG), USER_ID, _db_factory()
    )

    assert refreshed is True
    assert payload["config_data"] == CONVERTED_CONFIG
    # The caller's original object must not be mutated in place.
    assert "blocks" in _payload(LEGACY_BLOCKS_CONFIG)["config_data"]


async def test_identical_config_is_not_rewritten(fake_crud):
    """The common case: nothing changed, so nothing is claimed to have."""
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)
    original = _payload(CONVERTED_CONFIG)

    payload, refreshed = await refresh_payload_config_from_db(
        original, USER_ID, _db_factory()
    )

    assert refreshed is False
    assert payload is original


async def test_missing_db_row_keeps_the_cached_config(fake_crud):
    """A deleted config must not silently drop a running strategy."""
    payload, refreshed = await refresh_payload_config_from_db(
        _payload(LEGACY_BLOCKS_CONFIG), USER_ID, _db_factory()
    )

    assert refreshed is False
    assert payload["config_data"] == LEGACY_BLOCKS_CONFIG


async def test_db_outage_never_raises(fake_crud):
    """Rehydrate must degrade, not abort: losing every strategy is worse."""
    fake_crud.raise_on_call = True

    payload, refreshed = await refresh_payload_config_from_db(
        _payload(LEGACY_BLOCKS_CONFIG), USER_ID, _db_factory()
    )

    assert refreshed is False
    assert payload["config_data"] == LEGACY_BLOCKS_CONFIG


async def test_non_numeric_user_id_degrades_instead_of_raising(fake_crud):
    payload, refreshed = await refresh_payload_config_from_db(
        _payload(LEGACY_BLOCKS_CONFIG), "not-a-number", _db_factory()
    )

    assert refreshed is False
    assert payload["config_data"] == LEGACY_BLOCKS_CONFIG
    assert fake_crud.calls == [], "must not query with an invalid user id"


async def test_json_string_config_data_is_accepted(fake_crud):
    """A hand-written or legacy row can arrive as JSON text, not a dict."""
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(json.dumps(CONVERTED_CONFIG))

    payload, refreshed = await refresh_payload_config_from_db(
        _payload(LEGACY_BLOCKS_CONFIG), USER_ID, _db_factory()
    )

    assert refreshed is True
    assert payload["config_data"] == CONVERTED_CONFIG


async def test_corrupt_config_data_keeps_the_cached_copy(fake_crud):
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row("{not json")

    payload, refreshed = await refresh_payload_config_from_db(
        _payload(LEGACY_BLOCKS_CONFIG), USER_ID, _db_factory()
    )

    assert refreshed is False
    assert payload["config_data"] == LEGACY_BLOCKS_CONFIG


async def test_payload_without_a_config_id_is_left_alone(fake_crud):
    payload = {"mode": "paper", "config_data": LEGACY_BLOCKS_CONFIG}

    result, refreshed = await refresh_payload_config_from_db(
        payload, USER_ID, _db_factory()
    )

    assert refreshed is False
    assert result["config_data"] == LEGACY_BLOCKS_CONFIG
    assert fake_crud.calls == []


async def test_config_id_is_read_from_id_when_config_id_is_absent(fake_crud):
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)
    payload = {"id": CONFIG_ID, "config_data": LEGACY_BLOCKS_CONFIG}

    result, refreshed = await refresh_payload_config_from_db(
        payload, USER_ID, _db_factory()
    )

    assert refreshed is True
    assert result["config_data"] == CONVERTED_CONFIG


async def test_symbol_drift_is_refused_rather_than_half_applied(
    fake_crud, caplog
):
    """
    config_data pins the symbol the subscriber and the event matcher both honour
    (controller.py: "a pinned symbol is a CONSTRAINT, not a pass"), while
    `symbols` / `symbol_selection_mode` stay on the launch-time values. Swapping
    only config_data would evaluate the new instrument's conditions against the
    old instrument's stream.

    So drift must leave the cached payload whole -- it is at least internally
    consistent. Applying a symbol change is an API restart, which re-derives
    both halves together.
    """
    drifted = dict(CONVERTED_CONFIG, symbol="BTCUSDT")
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(drifted)

    with caplog.at_level(logging.WARNING):
        payload, refreshed = await refresh_payload_config_from_db(
            _payload(CONVERTED_CONFIG), USER_ID, _db_factory()
        )

    assert refreshed is False
    assert payload["config_data"]["symbol"] == "ETHUSDT"
    assert payload["config_data"] == CONVERTED_CONFIG, (
        "the cached payload must survive drift intact, not be half-updated"
    )
    assert "REFUSING to refresh" in caplog.text
    assert "Restart this strategy through the API" in caplog.text


async def test_a_failing_writeback_does_not_abort_the_rehydrate_loop(
    fake_crud, monkeypatch
):
    """
    The Redis SET is a third failure point that did not exist before this
    change. If it can raise out of the loop, every strategy after the first
    stale one is dropped on that boot.
    """
    from bot_module.controller import TradingController

    other_id = "be485ab9-384a-4adb-9f9b-a2df412d6db0"
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)
    fake_crud.rows[(USER_ID, other_id)] = _Row(CONVERTED_CONFIG)

    redis_client = _FakeRedis(
        {
            "running_strategies:1": {CONFIG_ID, other_id},
            f"running_strategy_payload:{USER_ID}:{CONFIG_ID}": json.dumps(
                _payload(LEGACY_BLOCKS_CONFIG)
            ),
            f"running_strategy_payload:{USER_ID}:{other_id}": json.dumps(
                _payload(LEGACY_BLOCKS_CONFIG, config_id=other_id)
            ),
        }
    )

    async def _boom(key, value):
        raise RuntimeError("simulated Redis ACL failure")

    monkeypatch.setattr(redis_client, "set", _boom)

    await TradingController._rehydrate_running_strategies_from_redis(
        _StubController(redis_client, _db_factory())
    )

    assert len(redis_client.published) == 2, (
        "one failing cache write must not cost the other strategy its restart"
    )
    published_ids = {
        json.loads(message)["payload"]["config_id"]
        for _, message in redis_client.published
    }
    assert published_ids == {CONFIG_ID, other_id}
    for _, message in redis_client.published:
        assert "blocks" not in json.loads(message)["payload"]["config_data"]


async def test_stale_refresh_is_logged_at_warning_level(fake_crud, caplog):
    """
    A restart silently reverting a config is the defect. The refresh must be
    visible, not a quiet correction.
    """
    fake_crud.rows[(USER_ID, CONFIG_ID)] = _Row(CONVERTED_CONFIG)

    with caplog.at_level(logging.WARNING):
        await refresh_payload_config_from_db(
            _payload(LEGACY_BLOCKS_CONFIG), USER_ID, _db_factory()
        )

    assert "STALE in Redis" in caplog.text