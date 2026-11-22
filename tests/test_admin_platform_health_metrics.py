"""The admin "Platform Health" card must never show a number nobody measured.

``GET /api/v1/admin/health/metrics`` used to answer with four constants::

    {"average_response_time_ms": 22.0, "uptime_30_days_percent": 99.98,
     "total_requests_24h": 1234567, "error_rate_24h": 0.02}

They were literals, not measurements, and the owner's admin dashboard showed
them as if they were real -- ``1,234,567`` is the literal placeholder. These
tests pin the replacement:

* a value is published only when it was actually measured;
* a value that cannot be measured is published as ``None`` with a reason;
* a broken metrics store publishes nothing at all (fail closed);
* the clients render a ``None`` as "Not measured", never as ``0``.

The metrics themselves (the recorder, the heartbeat, the arithmetic) are
covered by ``tests/test_system_metrics_recorder.py``.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from api.routes.admin import get_system_metrics
from api.schemas import SystemMetrics

REPO_ROOT = Path(__file__).resolve().parents[1]

# The constants the endpoint used to publish.
OLD_PLACEHOLDERS = {
    "average_response_time_ms": 22.0,
    "uptime_30_days_percent": 99.98,
    "total_requests_24h": 1234567,
    "error_rate_24h": 0.02,
}

METRIC_FIELDS = tuple(OLD_PLACEHOLDERS)

CLIENT_PAGES = (
    "frontend/src/pages/admin/AdminDashboardPage.tsx",
    "frontend/src/pages/admin/PlatformHealthPage.tsx",
)

CLIENT_HELPER = "frontend/src/lib/formatSystemMetric.ts"


class FakeMetricsStore:
    """A Redis double that speaks only the commands the recorder uses.

    It keeps the real counter semantics (HINCRBY accumulates, HSET reports
    whether the field was new) so the tests exercise the production code
    path rather than a copy of it.
    """

    def __init__(self, *, broken: bool = False) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.strings: Dict[str, str] = {}
        self.broken = broken
        self.expiries: List[str] = []

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("metrics store is down")

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        self._check()
        bucket = self.hashes.setdefault(key, {})
        value = int(bucket.get(field, 0)) + int(amount)
        bucket[field] = str(value)
        return value

    async def hset(self, key: str, field: str, value: str) -> int:
        self._check()
        bucket = self.hashes.setdefault(key, {})
        created = 0 if field in bucket else 1
        bucket[field] = value
        return created

    async def hgetall(self, key: str) -> Dict[str, str]:
        self._check()
        return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, ttl: int) -> bool:
        self._check()
        self.expiries.append(key)
        return True

    async def get(self, key: str) -> Optional[str]:
        self._check()
        return self.strings.get(key)

    async def set(self, key: str, value: str, nx: bool = False) -> Optional[bool]:
        self._check()
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def seed_heartbeat(store: FakeMetricsStore, start: datetime, end: datetime) -> None:
    """Mark every minute in [start, end) as observed by the heartbeat."""
    moment = start
    while moment < end:
        key = f"metrics:uptime:{moment.strftime('%Y%m%d')}"
        store.hashes.setdefault(key, {})[moment.strftime("%H%M")] = "1"
        moment += timedelta(minutes=1)


def seed_requests(
    store: FakeMetricsStore,
    minute: datetime,
    *,
    requests: int,
    errors: int = 0,
    duration_ms: float = 0.0,
    recorder_errors: int = 0,
) -> None:
    """Write one minute of request counters, as the recorder would."""
    key = f"metrics:req:{minute.strftime('%Y%m%d')}"
    bucket = store.hashes.setdefault(key, {})
    hhmm = minute.strftime("%H%M")
    bucket[f"r:{hhmm}"] = str(requests)
    bucket[f"e:{hhmm}"] = str(errors)
    bucket[f"d:{hhmm}"] = str(int(round(duration_ms * 1000)))
    if recorder_errors:
        bucket[f"x:{hhmm}"] = str(recorder_errors)


def seed_full_history(store: FakeMetricsStore, now: datetime) -> None:
    """Give the heartbeat a history that covers every window the API looks at.

    Deliberately wider than the windows themselves (31 days instead of 30,
    and it runs past "now") so a minute boundary crossing during the test
    cannot make a covered window look uncovered.
    """
    seed_heartbeat(store, now - timedelta(days=31), now + timedelta(minutes=5))


# --- The endpoint -------------------------------------------------------


async def test_endpoint_never_publishes_the_old_placeholder_constants():
    """The literal 22.0 / 99.98 / 1234567 / 0.02 must never come back."""
    store = FakeMetricsStore()
    seed_full_history(store, _now())
    seed_requests(store, _now() - timedelta(hours=2), requests=5)

    result = await get_system_metrics(redis_client=store)

    for field, placeholder in OLD_PLACEHOLDERS.items():
        assert result[field] != placeholder, f"{field} is back to its placeholder"


async def test_endpoint_reports_unmeasured_when_nothing_was_recorded():
    """No recorded minutes means no published numbers -- and a reason."""
    store = FakeMetricsStore()

    result = await get_system_metrics(redis_client=store)

    for field in METRIC_FIELDS:
        assert result[field] is None, f"{field} must be null, not {result[field]}"
        assert result["notes"].get(field), f"{field} must explain why it is null"


async def test_endpoint_fails_closed_when_the_metrics_store_is_broken():
    """A broken store must produce nulls, never a fallback number."""
    store = FakeMetricsStore(broken=True)

    result = await get_system_metrics(redis_client=store)

    for field in METRIC_FIELDS:
        assert result[field] is None, f"{field} must be null when the store is down"
    assert any("could not be read" in note for note in result["notes"].values())


async def test_endpoint_publishes_values_that_were_actually_measured():
    """Measured in, measured out -- and the arithmetic is checked by hand."""
    now = _now()
    store = FakeMetricsStore()
    seed_full_history(store, now)
    # Two minutes of real traffic, well inside the 24-hour window.
    seed_requests(
        store,
        now - timedelta(hours=2),
        requests=100,
        errors=2,
        duration_ms=100 * 50,  # 100 responses of 50 ms
    )
    seed_requests(
        store,
        now - timedelta(hours=3),
        requests=40,
        errors=0,
        duration_ms=40 * 20,  # 40 responses of 20 ms
    )

    result = await get_system_metrics(redis_client=store)

    assert result["total_requests_24h"] == 140
    # 2 failures out of 140 handled requests -> 1.4286%
    assert result["error_rate_24h"] == pytest.approx(1.4286, abs=1e-4)
    # (100*50 + 40*20) / 140 = 41.428571... ms
    assert result["average_response_time_ms"] == pytest.approx(41.429, abs=1e-3)
    # The old placeholder would have been 1234567 requests.
    assert result["total_requests_24h"] != 1234567


async def test_endpoint_does_not_publish_a_rate_over_zero_requests():
    """0/0 is not 0%. A zero error rate would read as "perfectly healthy"."""
    now = _now()
    store = FakeMetricsStore()
    seed_full_history(store, now)

    result = await get_system_metrics(redis_client=store)

    assert result["total_requests_24h"] == 0
    assert result["error_rate_24h"] is None
    assert result["average_response_time_ms"] is None
    assert "no requests were handled" in result["notes"]["error_rate_24h"]


async def test_endpoint_refuses_to_publish_when_the_recorder_failed():
    """A recorder failure inside the window means the counters are incomplete."""
    now = _now()
    store = FakeMetricsStore()
    seed_full_history(store, now)
    seed_requests(
        store,
        now - timedelta(hours=2),
        requests=100,
        errors=1,
        duration_ms=100 * 10,
        recorder_errors=3,
    )

    result = await get_system_metrics(redis_client=store)

    assert result["total_requests_24h"] is None
    assert result["error_rate_24h"] is None
    assert result["average_response_time_ms"] is None
    assert "failed" in result["notes"]["total_requests_24h"]


async def test_endpoint_reports_uptime_as_unmeasured_until_the_window_is_full():
    """A 30-day figure built from 2 days of history would be a lie."""
    now = _now()
    store = FakeMetricsStore()
    seed_heartbeat(store, now - timedelta(days=2), now)

    result = await get_system_metrics(redis_client=store)

    assert result["uptime_30_days_percent"] is None
    assert "30-day window" in result["notes"]["uptime_30_days_percent"]


async def test_endpoint_publishes_uptime_once_the_whole_window_is_covered():
    """A fully observed window gives a real uptime figure."""
    now = _now()
    store = FakeMetricsStore()
    seed_full_history(store, now)

    result = await get_system_metrics(redis_client=store)

    assert result["uptime_30_days_percent"] == pytest.approx(100.0, abs=0.01)
    assert result["notes"]["uptime_30_days_percent"]


async def test_endpoint_counts_a_missing_minute_as_downtime():
    """A minute with no heartbeat is downtime, and it lowers the figure."""
    now = _now()
    store = FakeMetricsStore()
    seed_full_history(store, now)
    # Punch a one-minute hole in the middle of the 30-day window.
    hole = now - timedelta(days=10)
    key = f"metrics:uptime:{hole.strftime('%Y%m%d')}"
    store.hashes[key].pop(hole.strftime("%H%M"), None)

    result = await get_system_metrics(redis_client=store)

    assert result["uptime_30_days_percent"] is not None
    assert result["uptime_30_days_percent"] < 100.0
    assert "downtime" in result["notes"]["uptime_30_days_percent"]


async def test_the_uptime_note_says_which_process_the_heartbeat_covers():
    """The heartbeat sees the API process, not the whole platform.

    The API, the trading bot, the market-data worker and the Celery workers
    are separate containers. A note that said only "downtime" would let an
    operator read an API-only measurement as platform uptime -- the same
    class of overclaim this whole change exists to remove.
    """
    now = _now()
    store = FakeMetricsStore()
    seed_full_history(store, now)

    result = await get_system_metrics(redis_client=store)

    note = result["notes"]["uptime_30_days_percent"]
    assert "API process" in note
    assert "not cover" in note


# --- The schema ---------------------------------------------------------


def test_the_schema_accepts_an_unmeasured_metric():
    """A null must survive validation -- that is how "not measured" travels."""
    metrics = SystemMetrics(
        average_response_time_ms=None,
        uptime_30_days_percent=None,
        total_requests_24h=None,
        error_rate_24h=None,
        notes={"error_rate_24h": "not measured"},
    )

    assert metrics.average_response_time_ms is None
    assert metrics.uptime_30_days_percent is None
    assert metrics.total_requests_24h is None
    assert metrics.error_rate_24h is None
    assert metrics.notes["error_rate_24h"] == "not measured"


# --- The clients --------------------------------------------------------


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", CLIENT_PAGES)
def test_admin_page_never_formats_an_unmeasured_metric_as_a_number(page):
    """A null metric must not reach toFixed()/toLocaleString()."""
    source = _read(page)

    for field in METRIC_FIELDS:
        assert not re.search(rf"{field}\s*\.\s*toFixed\(", source), (
            f"{page} formats {field} directly; a null would crash or lie"
        )
        assert not re.search(rf"{field}\s*\.\s*toLocaleString\(", source), (
            f"{page} formats {field} directly; a null would crash or lie"
        )
        assert re.search(
            rf"formatMetric\(\s*\w+\s*\.\s*{field}\b", source
        ), f"{page} must render {field} through formatMetric()"


def test_the_client_helper_says_not_measured_for_a_missing_metric():
    """The visible label, and the one branch that returns it."""
    helper = _read(CLIENT_HELPER)

    assert 'NOT_MEASURED_LABEL = "Not measured"' in helper
    assert "return NOT_MEASURED_LABEL;" in helper


def test_the_client_helper_gives_an_unmeasured_metric_no_verdict():
    """An unmeasured metric must not be coloured as if it were healthy."""
    helper = _read(CLIENT_HELPER)

    assert 'return "unknown";' in helper
    assert "text-muted-foreground" in helper
