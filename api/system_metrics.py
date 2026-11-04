# api/system_metrics.py
"""Measured platform metrics for the admin console.

``GET /api/v1/admin/health/metrics`` used to answer with four constants::

    {"average_response_time_ms": 22.0, "uptime_30_days_percent": 99.98,
     "total_requests_24h": 1234567, "error_rate_24h": 0.02}

They were literals under a ``# TODO: Replace with real metric gathering logic``
comment -- not measurements of anything. An operator console that always
reports "healthy" is worse than one that admits it is blind, so the rule
enforced by this module is absolute:

    **Publish only what was measured. Everything else is None, with a reason.**

Every number published here is derived from counters this service writes
itself:

``metrics:req:<YYYYMMDD>``
    A hash with one field per minute of that day: ``r:<HHMM>`` requests
    handled, ``e:<HHMM>`` responses that were 5xx, ``d:<HHMM>`` total
    response time in microseconds, ``x:<HHMM>`` recorder failures. Written by
    :class:`RequestMetricsMiddleware`.

``metrics:uptime:<YYYYMMDD>``
    A hash with one field per minute of that day (``<HHMM>``). Written by
    :func:`heartbeat_loop`, which runs inside the API process -- so a minute
    without a heartbeat is a minute the platform was not demonstrably up.

``metrics:uptime:started_at``
    When the heartbeat first ran, so the console can say *how long* it has
    been measuring instead of just "unknown".

Failure policy: **fail closed**. If the metrics store cannot be read, every
metric is reported as unmeasured. A monitoring surface that falls back to a
default when it is broken is a monitoring surface that lies exactly when it
matters most.

Coverage policy: **a window is only published when the recorder was running
for the whole of it.** The heartbeat and the request recorder live in the
same process, so the heartbeat's minute markers are the proof that the
request counters were being written throughout the window. Without that proof
a 24-hour figure would quietly be a 2-hour figure wearing a 24-hour label.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Set, Tuple

import redis.asyncio as aioredis
from starlette.types import ASGIApp, Receive, Scope, Send

from bot_module import config as bot_config

logger = logging.getLogger(__name__)

# --- Redis key layout ---------------------------------------------------

REQUEST_DAY_PREFIX = "metrics:req:"
HEARTBEAT_DAY_PREFIX = "metrics:uptime:"
HEARTBEAT_STARTED_AT_KEY = "metrics:uptime:started_at"

# --- Windows -----------------------------------------------------------

WINDOW_24H_MINUTES = 24 * 60
WINDOW_30D_MINUTES = 30 * 24 * 60

# --- Retention ---------------------------------------------------------

# A request day-hash has to outlive the 24-hour window it feeds, including
# the day it straddles, so it is kept for a day plus three hours of slack.
# The heartbeat day-hashes have to outlive the 30-day uptime window.
REQUEST_DAY_TTL_SECONDS = 27 * 3600
HEARTBEAT_DAY_TTL_SECONDS = 35 * 24 * 3600

# --- Heartbeat cadence -------------------------------------------------

# The heartbeat marks the current minute AND the previous one. Marking the
# previous minute is what keeps a restart from punching a hole in the
# history: without it, a process that died at 13:04:59 and came back at
# 13:05:01 would leave 13:04 looking like an outage.
HEARTBEAT_TICK_SECONDS = 15
HEARTBEAT_CATCHUP_MINUTES = 1

METRIC_FIELDS = (
    "average_response_time_ms",
    "uptime_30_days_percent",
    "total_requests_24h",
    "error_rate_24h",
)

# A response is an error when the server failed, not when the client did.
ERROR_STATUS_THRESHOLD = 500


class MetricsStoreError(RuntimeError):
    """The metrics store did not answer in a shape this module understands."""


# --- Small time helpers ------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(moment: datetime) -> str:
    return moment.strftime("%Y%m%d")


def _minute_key(moment: datetime) -> str:
    return moment.strftime("%Y%m%d%H%M")


def _floor_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


# --- Writing the measurements -----------------------------------------


async def record_request(
    redis_client: aioredis.Redis,
    *,
    status_code: int,
    duration_ms: float,
    now: Optional[datetime] = None,
) -> None:
    """Record one handled HTTP request into the current minute's counters.

    Duration is stored in microseconds because the console reports
    milliseconds and an integer-millisecond counter would round a 0.4 ms
    response to 0.
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    hhmm = moment.strftime("%H%M")
    key = REQUEST_DAY_PREFIX + _day_key(moment)

    await redis_client.hincrby(key, f"r:{hhmm}", 1)
    await redis_client.hincrby(key, f"d:{hhmm}", int(round(duration_ms * 1000)))
    if status_code >= ERROR_STATUS_THRESHOLD:
        await redis_client.hincrby(key, f"e:{hhmm}", 1)
    await redis_client.expire(key, REQUEST_DAY_TTL_SECONDS)


async def record_heartbeat(
    redis_client: aioredis.Redis, *, now: Optional[datetime] = None
) -> None:
    """Mark the current minute (and the previous one) as observed.

    Marking a minute means "the API process was alive during it". It does not
    mean the platform was busy -- an idle platform is still up, and uptime
    must not be confused with activity.
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    for offset in range(HEARTBEAT_CATCHUP_MINUTES + 1):
        marked = moment - timedelta(minutes=offset)
        key = HEARTBEAT_DAY_PREFIX + _day_key(marked)
        await redis_client.hset(key, marked.strftime("%H%M"), "1")
        await redis_client.expire(key, HEARTBEAT_DAY_TTL_SECONDS)

    # NX so the first heartbeat ever wins and the answer stays "we have been
    # measuring since <that moment>" instead of drifting forward.
    await redis_client.set(HEARTBEAT_STARTED_AT_KEY, moment.isoformat(), nx=True)


async def heartbeat_loop(
    redis_client: aioredis.Redis, *, interval: int = HEARTBEAT_TICK_SECONDS
) -> None:
    """Write the heartbeat until cancelled.

    Every failure is swallowed and retried: a heartbeat that dies on the
    first Redis hiccup would leave a permanent hole in the uptime history.
    """
    while True:
        try:
            await record_heartbeat(redis_client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - depends on Redis
            logger.warning("heartbeat write failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)


# --- Reading the measurements back -------------------------------------


async def _hgetall(redis_client: aioredis.Redis, key: str) -> Dict[str, str]:
    """Read a hash and refuse to guess if the reply is not a hash.

    A non-dict reply means the store is not behaving (or a test double is in
    the way). Guessing at that point is how a monitoring surface starts
    inventing numbers, so this raises instead.
    """
    raw = await redis_client.hgetall(key)
    if not isinstance(raw, dict):
        raise MetricsStoreError(
            f"expected a hash for {key}, got {type(raw).__name__}"
        )
    return {str(field): value for field, value in raw.items()}


async def _read_heartbeat_days(
    redis_client: aioredis.Redis, start: datetime, end: datetime
) -> Dict[str, Set[str]]:
    """Return {day key -> set of 'HHMM' minutes observed} for [start, end)."""
    days: Dict[str, Set[str]] = {}
    day = start.date()
    last = end.date()
    while day <= last:
        key = HEARTBEAT_DAY_PREFIX + day.strftime("%Y%m%d")
        raw = await _hgetall(redis_client, key)
        markers = {
            field
            for field in raw
            if len(field) == 4 and field.isdigit()
        }
        days[day.strftime("%Y%m%d")] = markers
        day += timedelta(days=1)
    return days


async def _read_request_buckets(
    redis_client: aioredis.Redis, start: datetime, end: datetime
) -> Dict[str, Dict[str, int]]:
    """Return {minute key -> counters} for the minutes in [start, end)."""
    buckets: Dict[str, Dict[str, int]] = {}
    day = start.date()
    last = end.date()
    while day <= last:
        key = REQUEST_DAY_PREFIX + day.strftime("%Y%m%d")
        raw = await _hgetall(redis_client, key)
        day_key = day.strftime("%Y%m%d")
        for field, value in raw.items():
            kind, _, hhmm = field.partition(":")
            if kind not in ("r", "e", "d", "x") or len(hhmm) != 4:
                continue
            try:
                amount = int(float(value))
            except (TypeError, ValueError):
                continue
            entry = buckets.setdefault(
                day_key + hhmm,
                {"requests": 0, "errors": 0, "duration_us": 0, "recorder_errors": 0},
            )
            if kind == "r":
                entry["requests"] += amount
            elif kind == "e":
                entry["errors"] += amount
            elif kind == "d":
                entry["duration_us"] += amount
            else:
                entry["recorder_errors"] += amount
        day += timedelta(days=1)
    return buckets


def _count_observed_minutes(
    days: Mapping[str, Set[str]], start: datetime, end: datetime
) -> int:
    """How many minutes in [start, end) carry a heartbeat marker."""
    total = 0
    for day_key, markers in days.items():
        day = datetime.strptime(day_key, "%Y%m%d").date()
        low = start.strftime("%H%M") if day == start.date() else "0000"
        high = end.strftime("%H%M") if day == end.date() else "2400"
        for marker in markers:
            if low <= marker < high:
                total += 1
    return total


def _marker_present(days: Mapping[str, Set[str]], moment: datetime) -> bool:
    """Was this exact minute observed by the heartbeat?"""
    return moment.strftime("%H%M") in days.get(_day_key(moment), set())


def _sum_window(
    buckets: Mapping[str, Mapping[str, int]], start: datetime, minutes: int
) -> Dict[str, int]:
    """Add up the request counters over a window of completed minutes."""
    totals = {
        "requests": 0,
        "errors": 0,
        "duration_us": 0,
        "recorder_errors": 0,
    }
    moment = start
    for _ in range(minutes):
        entry = buckets.get(_minute_key(moment))
        if entry:
            for field in totals:
                totals[field] += int(entry.get(field, 0) or 0)
        moment += timedelta(minutes=1)
    return totals


# --- The published view ------------------------------------------------


async def collect_system_metrics(
    redis_client: aioredis.Redis, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Read the real measurements and report anything missing as unmeasured.

    The returned dict matches :class:`api.schemas.SystemMetrics`: four
    nullable values plus a ``notes`` map explaining, per metric, either why it
    could not be measured or what the measurement covers.
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    floor = _floor_minute(moment)
    window_24h_start = floor - timedelta(minutes=WINDOW_24H_MINUTES)
    window_30d_start = floor - timedelta(minutes=WINDOW_30D_MINUTES)

    result: Dict[str, Any] = {
        "average_response_time_ms": None,
        "uptime_30_days_percent": None,
        "total_requests_24h": None,
        "error_rate_24h": None,
        "notes": {},
    }
    notes: Dict[str, str] = result["notes"]

    try:
        heartbeat_days = await _read_heartbeat_days(
            redis_client, window_30d_start, floor
        )
        request_buckets = await _read_request_buckets(
            redis_client, window_24h_start, floor
        )
        started_at = await redis_client.get(HEARTBEAT_STARTED_AT_KEY)
    except Exception as exc:
        logger.error(
            "admin system metrics: metrics store unavailable: %s: %s",
            type(exc).__name__,
            exc,
        )
        reason = (
            "the metrics store could not be read "
            f"({type(exc).__name__}); no figure is published because none "
            "can be trusted"
        )
        for field in METRIC_FIELDS:
            notes[field] = reason
        return result

    # --- The 24-hour window -------------------------------------------
    #
    # The heartbeat and the request recorder are the same process, so a
    # heartbeat marker at the start of the window is the proof that the
    # request counters were being written for the whole of it. Without that
    # proof a "24 hour" figure could be two hours of traffic wearing a
    # 24-hour label.
    if not _marker_present(heartbeat_days, window_24h_start):
        reason = (
            "the request recorder has not been running for the whole "
            "24-hour window yet"
        )
        if started_at:
            reason += f" (measuring since {started_at})"
        for field in ("total_requests_24h", "error_rate_24h", "average_response_time_ms"):
            notes[field] = reason
    else:
        totals = _sum_window(request_buckets, window_24h_start, WINDOW_24H_MINUTES)
        if totals["recorder_errors"]:
            reason = (
                "the request recorder failed in "
                f"{totals['recorder_errors']} minute(s) inside the window, so "
                "the counters are incomplete"
            )
            for field in ("total_requests_24h", "error_rate_24h", "average_response_time_ms"):
                notes[field] = reason
        else:
            handled = totals["requests"]
            result["total_requests_24h"] = handled
            notes["total_requests_24h"] = (
                "requests the API handled in the last 24 hours, counted per minute"
            )
            if handled > 0:
                result["error_rate_24h"] = round(
                    totals["errors"] / handled * 100, 4
                )
                result["average_response_time_ms"] = round(
                    totals["duration_us"] / handled / 1000, 3
                )
                notes["error_rate_24h"] = (
                    "share of those responses that were 5xx"
                )
                notes["average_response_time_ms"] = (
                    "mean time the API took to answer those requests"
                )
            else:
                # 0/0 is not 0%. A zero error rate over no requests would
                # read as "perfectly healthy" and mean nothing at all.
                notes["error_rate_24h"] = (
                    "no requests were handled in the last 24 hours, so the "
                    "error rate is undefined rather than zero"
                )
                notes["average_response_time_ms"] = (
                    "no requests were handled in the last 24 hours, so the "
                    "average response time is undefined rather than zero"
                )

    # --- The 30-day uptime window -------------------------------------
    observed = _count_observed_minutes(heartbeat_days, window_30d_start, floor)
    if not _marker_present(heartbeat_days, window_30d_start):
        reason = (
            f"the heartbeat has observed {observed} of {WINDOW_30D_MINUTES} "
            "minutes of the 30-day window; a full window is required before "
            "an uptime figure means anything"
        )
        if started_at:
            reason += f" (measuring since {started_at})"
        notes["uptime_30_days_percent"] = reason
    else:
        result["uptime_30_days_percent"] = round(
            observed / WINDOW_30D_MINUTES * 100, 4
        )
        missing = WINDOW_30D_MINUTES - observed
        notes["uptime_30_days_percent"] = (
            f"the heartbeat observed {observed} of {WINDOW_30D_MINUTES} "
            f"minutes; {missing} minute(s) had no heartbeat and count as downtime"
        )

    return result


# --- The request recorder ----------------------------------------------


_shared_client: Optional[aioredis.Redis] = None


def get_metrics_redis(app: Any = None) -> aioredis.Redis:
    """The Redis client the recorder uses.

    Prefers ``app.state.metrics_redis`` so the application can own the
    connection (and so a test can inject a double); falls back to a shared
    client with short timeouts.
    """
    state = getattr(app, "state", None)
    client = getattr(state, "metrics_redis", None)
    if client is not None:
        return client

    global _shared_client
    if _shared_client is None:
        _shared_client = aioredis.Redis(
            host=bot_config.REDIS_HOST,
            port=bot_config.REDIS_PORT,
            db=bot_config.REDIS_DB,
            username=bot_config.REDIS_USERNAME,
            password=bot_config.REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
    return _shared_client


class RequestMetricsMiddleware:
    """Counts the requests this process handled, per minute.

    Pure ASGI (like :class:`api.main.BodySizeLimitMiddleware`) so it does not
    buffer the body or reorder the response.

    It never fails a request: a metrics store that is down must not take the
    API down with it. A failed write is recorded as a recorder failure so
    the console can refuse to publish counters it knows are incomplete.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        state = {"status": ERROR_STATUS_THRESHOLD}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status", ERROR_STATUS_THRESHOLD))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            try:
                await record_request(
                    get_metrics_redis(scope.get("app")),
                    status_code=state["status"],
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                # Best effort: count the failure if the store is reachable
                # enough to take it, otherwise stay quiet. Either way the
                # request itself already succeeded or failed on its own terms.
                logger.debug("request metric write failed: %s: %s", type(exc).__name__, exc)
                await self._record_recorder_failure(scope, exc)

    async def _record_recorder_failure(self, scope: Scope, exc: Exception) -> None:
        moment = _utcnow()
        hhmm = moment.strftime("%H%M")
        key = REQUEST_DAY_PREFIX + _day_key(moment)
        try:
            # HSET, not HINCRBY: the counter write is the thing most likely to
            # have failed, so the failure marker must not need it. A marker
            # only has to say "this minute is unreliable", not how many times.
            await get_metrics_redis(scope.get("app")).hset(key, f"x:{hhmm}", "1")
            await get_metrics_redis(scope.get("app")).expire(
                key, REQUEST_DAY_TTL_SECONDS
            )
        except Exception:
            logger.debug(
                "could not record the request-metric failure: %s: %s",
                type(exc).__name__,
                exc,
            )
