"""The measurement layer behind the admin "Platform Health" card.

These tests cover the machinery that produces the numbers the admin console
publishes: the per-minute request counters, the heartbeat that says "the API
process was alive during this minute", and the middleware that feeds them.

They are deliberately independent of ``tests/test_admin_platform_health_metrics.py``
so that the endpoint test file keeps collecting against a tree where the
measurement layer does not exist yet -- that is what makes the RED proof of
the endpoint test meaningful.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import pytest

from api.system_metrics import (
    HEARTBEAT_DAY_PREFIX,
    HEARTBEAT_STARTED_AT_KEY,
    REQUEST_DAY_PREFIX,
    RequestMetricsMiddleware,
    collect_system_metrics,
    heartbeat_loop,
    record_heartbeat,
    record_request,
)


class FakeRedis:
    """A Redis double that speaks only the commands the recorder uses."""

    def __init__(self, *, broken_commands: Optional[Set[str]] = None) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.strings: Dict[str, str] = {}
        self.broken_commands: Set[str] = broken_commands or set()
        self.expiries: List[str] = []

    def _check(self, command: str) -> None:
        if command in self.broken_commands:
            raise ConnectionError(f"{command} failed")

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        self._check("hincrby")
        bucket = self.hashes.setdefault(key, {})
        value = int(bucket.get(field, 0)) + int(amount)
        bucket[field] = str(value)
        return value

    async def hset(self, key: str, field: str, value: str) -> int:
        self._check("hset")
        bucket = self.hashes.setdefault(key, {})
        created = 0 if field in bucket else 1
        bucket[field] = value
        return created

    async def hgetall(self, key: str) -> Dict[str, str]:
        self._check("hgetall")
        return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, ttl: int) -> bool:
        self._check("expire")
        self.expiries.append(key)
        return True

    async def get(self, key: str) -> Optional[str]:
        self._check("get")
        return self.strings.get(key)

    async def set(self, key: str, value: str, nx: bool = False) -> Optional[bool]:
        self._check("set")
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


# --- The request recorder ------------------------------------------------


async def test_record_request_counts_requests_and_duration():
    store = FakeRedis()
    moment = datetime(2026, 11, 4, 13, 5, tzinfo=timezone.utc)

    await record_request(store, status_code=200, duration_ms=12.5, now=moment)

    bucket = store.hashes[REQUEST_DAY_PREFIX + "20261104"]
    assert bucket["r:1305"] == "1"
    # Microseconds, so a sub-millisecond response does not round to zero.
    assert bucket["d:1305"] == "12500"
    assert "e:1305" not in bucket


async def test_record_request_counts_a_server_error_but_not_a_client_error():
    store = FakeRedis()
    moment = datetime(2026, 11, 4, 13, 5, tzinfo=timezone.utc)

    await record_request(store, status_code=500, duration_ms=1, now=moment)
    await record_request(store, status_code=404, duration_ms=1, now=moment)

    bucket = store.hashes[REQUEST_DAY_PREFIX + "20261104"]
    assert bucket["r:1305"] == "2"
    assert bucket["e:1305"] == "1"


async def test_record_request_accumulates_within_the_same_minute():
    store = FakeRedis()
    moment = datetime(2026, 11, 4, 13, 5, tzinfo=timezone.utc)

    await record_request(store, status_code=200, duration_ms=10, now=moment)
    await record_request(store, status_code=200, duration_ms=30, now=moment)

    bucket = store.hashes[REQUEST_DAY_PREFIX + "20261104"]
    assert bucket["r:1305"] == "2"
    assert bucket["d:1305"] == "40000"


async def test_record_request_sets_an_expiry_so_the_counters_do_not_forever_grow():
    store = FakeRedis()
    moment = datetime(2026, 11, 4, 13, 5, tzinfo=timezone.utc)

    await record_request(store, status_code=200, duration_ms=1, now=moment)

    assert REQUEST_DAY_PREFIX + "20261104" in store.expiries


# --- The heartbeat -------------------------------------------------------


async def test_the_heartbeat_marks_the_current_and_the_previous_minute():
    store = FakeRedis()
    moment = datetime(2026, 11, 4, 13, 5, tzinfo=timezone.utc)

    await record_heartbeat(store, now=moment)

    markers = store.hashes[HEARTBEAT_DAY_PREFIX + "20261104"]
    # The previous minute is marked too: a process that restarted at 13:05:01
    # must not leave 13:04 looking like an outage.
    assert markers["1305"] == "1"
    assert markers["1304"] == "1"


async def test_the_heartbeat_remembers_when_it_first_ran():
    store = FakeRedis()
    first = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 4, 13, 5, tzinfo=timezone.utc)

    await record_heartbeat(store, now=first)
    await record_heartbeat(store, now=later)

    assert store.strings[HEARTBEAT_STARTED_AT_KEY] == first.isoformat()


async def test_the_heartbeat_loop_writes_and_survives_a_broken_store():
    broken = FakeRedis(broken_commands={"hset"})
    task = asyncio.create_task(heartbeat_loop(broken, interval=3600))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    healthy = FakeRedis()
    task = asyncio.create_task(heartbeat_loop(healthy, interval=3600))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert healthy.hashes  # a heartbeat was written


# --- The middleware ------------------------------------------------------


class _State:
    def __init__(self, redis_client) -> None:
        self.metrics_redis = redis_client


class _App:
    def __init__(self, redis_client) -> None:
        self.state = _State(redis_client)


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200})
    await send({"type": "http.response.body", "body": b"ok"})


async def _failing_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 500})
    await send({"type": "http.response.body", "body": b"boom"})


async def _run_middleware(app, redis_client, scope=None):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = RequestMetricsMiddleware(app)
    await middleware(
        scope or {"type": "http", "app": _App(redis_client)}, receive, send
    )
    return sent


async def test_the_middleware_records_the_request_it_handled():
    store = FakeRedis()

    sent = await _run_middleware(_ok_app, store)

    assert sent[0]["status"] == 200
    bucket = next(iter(store.hashes.values()))
    request_fields = [key for key in bucket if key.startswith("r:")]
    duration_fields = [key for key in bucket if key.startswith("d:")]
    assert len(request_fields) == 1
    assert bucket[request_fields[0]] == "1"
    assert int(bucket[duration_fields[0]]) > 0


async def test_the_middleware_records_a_server_error_as_an_error():
    store = FakeRedis()

    await _run_middleware(_failing_app, store)

    bucket = next(iter(store.hashes.values()))
    error_fields = [key for key in bucket if key.startswith("e:")]
    assert len(error_fields) == 1
    assert bucket[error_fields[0]] == "1"


async def test_the_middleware_never_fails_a_request_when_the_store_is_down():
    store = FakeRedis(broken_commands={"hincrby", "expire"})

    sent = await _run_middleware(_ok_app, store)

    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"ok"


async def test_the_middleware_notes_its_own_failures():
    """A recorder failure is recorded, so the console can refuse the counters."""
    store = FakeRedis(broken_commands={"hincrby"})

    await _run_middleware(_ok_app, store)

    bucket = next(iter(store.hashes.values()))
    failure_fields = [key for key in bucket if key.startswith("x:")]
    assert len(failure_fields) == 1
    assert bucket[failure_fields[0]] == "1"


async def test_the_middleware_leaves_non_http_scopes_alone():
    store = FakeRedis()
    called = {"websocket": False}

    async def websocket_app(scope, receive, send):
        called["websocket"] = True

    middleware = RequestMetricsMiddleware(websocket_app)
    await middleware({"type": "websocket", "app": _App(store)}, None, None)

    assert called["websocket"] is True
    assert store.hashes == {}


# --- The published view --------------------------------------------------


async def test_collect_system_metrics_never_raises_when_the_store_is_broken():
    store = FakeRedis(broken_commands={"hgetall"})

    result = await collect_system_metrics(store)

    assert result["total_requests_24h"] is None
    assert result["error_rate_24h"] is None
    assert result["average_response_time_ms"] is None
    assert result["uptime_30_days_percent"] is None
