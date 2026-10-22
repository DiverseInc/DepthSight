"""
Focused test: Redis pubsub pipeline between MDS and bot DataConsumer.

Verifies that:
1. DataConsumer subscribes to a Redis channel and receives published messages
2. The lock around get_message does NOT cause race conditions with subscribe
3. Multiple rapid subscriptions + publications work without data loss

This test specifically catches the bug where get_message WITHOUT lock
would consume the subscribe response meant for subscribe(), breaking
the pubsub connection permanently.

Run:
  python -m pytest tests/test_redis_pubsub_pipeline.py -v --noconftest
"""

import asyncio
import json
import logging

import pytest
import fakeredis.aioredis

logger = logging.getLogger(__name__)


@pytest.fixture
def shared_server():
    return fakeredis.FakeServer()


@pytest.fixture
def publisher(shared_server):
    """Separate Redis client acting as MDS."""
    return fakeredis.aioredis.FakeRedis(server=shared_server)


@pytest.mark.asyncio
async def test_pubsub_delivery(shared_server, publisher, monkeypatch):
    """
    Core test: DataConsumer subscribes, publishes, receives.
    """
    # Patch config + redis BEFORE importing DataConsumer
    _patch_config_and_redis(shared_server, monkeypatch)

    from bot_module.data_consumer import DataConsumer

    consumer = DataConsumer(
        loop=asyncio.get_running_loop(),
        executor=None,
        event_queue=None,
    )

    try:
        await consumer.start()
        assert consumer._redis_market_pubsub is not None

        # Track received payloads (decode bytes to dict if needed)
        received = []
        orig = consumer._handle_redis_market_payload

        async def track(msg):
            if isinstance(msg, bytes):
                msg = json.loads(msg.decode("utf-8"))
            received.append(msg)
            await orig(msg)

        consumer._handle_redis_market_payload = track

        # Subscribe — this calls _ensure_subscription_via_redis
        await consumer.ensure_subscription(
            data_type_key="kline_1m",
            symbol="BTCUSDT",
            market_type="futures_usdtm",
        )

        # Get whatever stream_key the DataConsumer generated
        keys = list(consumer._redis_market_stream_keys)
        assert len(keys) >= 1, f"Expected at least 1 stream key, got {keys}"
        stream_key = keys[0]

        await asyncio.sleep(0.2)

        # Publish as MDS would
        channel = f"depthsight:market_data:events:{stream_key}"
        payload = {
            "type": "market_payload",
            "stream_key": stream_key,
            "symbol": "BTCUSDT",
            "data_type_key": "kline_1m",
        }
        n = await publisher.publish(channel, json.dumps(payload))
        assert n >= 1, f"Redis publish returned {n} subscribers (expected >=1)"

        # Wait for listener to pick it up
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.1)

        assert len(received) >= 1, (
            f"No payload received despite Redis reporting {n} subscribers. "
            f"stream_keys={consumer._redis_market_stream_keys}"
        )
        assert received[0].get("stream_key") == stream_key
        assert received[0].get("type") == "market_payload"

    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_multiple_rapid_subscriptions(
    shared_server, publisher, monkeypatch
):
    """
    Subscribe to multiple channels while listener is polling get_message.
    This stress-tests the lock between get_message and subscribe.
    """
    _patch_config_and_redis(shared_server, monkeypatch)

    from bot_module.data_consumer import DataConsumer

    consumer = DataConsumer(
        loop=asyncio.get_running_loop(),
        executor=None,
        event_queue=None,
    )

    try:
        await consumer.start()

        received = []
        orig = consumer._handle_redis_market_payload

        async def track(msg):
            received.append(msg)
            await orig(msg)

        consumer._handle_redis_market_payload = track

        # Rapid sequential subscriptions to different symbols
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        for symbol in symbols:
            await consumer.ensure_subscription(
                data_type_key="kline_1m",
                symbol=symbol,
                market_type="futures_usdtm",
            )
            await asyncio.sleep(0.01)

        await asyncio.sleep(0.2)

        # Publish one message to each channel
        for symbol in symbols:
            stream_key = next(
                (k for k in consumer._redis_market_stream_keys if symbol.lower() in k),
                None,
            )
            assert stream_key, f"No stream_key found for {symbol}"
            channel = f"depthsight:market_data:events:{stream_key}"
            payload = {
                "type": "market_payload",
                "stream_key": stream_key,
                "symbol": symbol,
                "data_type_key": "kline_1m",
            }
            await publisher.publish(channel, json.dumps(payload))

        await asyncio.sleep(1.0)

        # Should have received at least 3 messages
        assert len(received) >= 3, (
            f"Expected 3+ payloads, got {len(received)}. "
            f"stream_keys={consumer._redis_market_stream_keys}"
        )

    finally:
        await consumer.stop()


def _patch_config_and_redis(server, monkeypatch):
    """Patch config and redis.asyncio.Redis *before* DataConsumer import.

    Every global below is applied through `monkeypatch`, so it is UNDONE when
    the test finishes. Assigning them directly leaked them into the rest of the
    session, and `redis.asyncio.Redis` is the one that actually broke tests.

    It stayed a plain factory function, so the `mock_redis_client` fixture in
    tests/conftest.py built `MagicMock(spec=<function>)` instead of
    `MagicMock(spec=Redis)`. That fixture promotes every callable it finds via
    `dir(redis.Redis)` into an AsyncMock, and `dir()` of a function yields
    nothing -- so every redis method outside conftest's hand-written list
    (`sadd` included) raised AttributeError for the rest of the run.

    Measured on tests/test_redis_pubsub_pipeline.py + the guard file:
    test_the_api_records_the_approval_state_at_launch failed ONLY when this
    file ran first, with `KeyError: running_strategy_payload:...`, because
    strategies.py's `await redis_client.sadd(...)` had raised and the route's
    `except Exception` swallowed it before the payload was cached. This file
    is collected immediately before the guard file, which is why the failure
    looked unrelated to it.
    """
    import numpy as np

    if not hasattr(np, "NaN"):
        monkeypatch.setattr(np, "NaN", np.nan, raising=False)

    import bot_module.config as cfg

    monkeypatch.setattr(cfg, "MARKET_DATA_FANOUT_MODE", "redis", raising=False)
    monkeypatch.setattr(
        cfg,
        "MARKET_DATA_REDIS_EVENT_CHANNEL_PREFIX",
        "depthsight:market_data:events",
        raising=False,
    )
    monkeypatch.setattr(cfg, "MARKET_REDIS_HOST", "fake", raising=False)
    monkeypatch.setattr(cfg, "MARKET_REDIS_PORT", 6379, raising=False)
    monkeypatch.setattr(cfg, "MARKET_REDIS_DB", 0, raising=False)
    monkeypatch.setattr(cfg, "REDIS_USERNAME", "bot", raising=False)
    monkeypatch.setattr(cfg, "REDIS_PASSWORD", "x", raising=False)
    monkeypatch.setattr(
        cfg,
        "MARKET_DATA_REDIS_COMMAND_CHANNEL",
        "depthsight:market_data:commands",
        raising=False,
    )
    monkeypatch.setattr(cfg, "BINANCE_DEPTH_STREAM_NAME", "@depth", raising=False)

    import bot_module.data_consumer as dc_mod

    # FakeRedis with decode_responses=True (matching DataConsumer's Redis init)
    def fake_factory(**kw):
        kw.setdefault("decode_responses", True)
        return fakeredis.aioredis.FakeRedis(server=server, **kw)

    # Same target as before -- `dc_mod.redis_asyncio` IS the redis.asyncio
    # module -- but now restorable. This is the leak that broke other files.
    monkeypatch.setattr(dc_mod.redis_asyncio, "Redis", fake_factory)
