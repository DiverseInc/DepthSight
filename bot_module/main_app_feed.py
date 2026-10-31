"""Is the configured MAIN_APP screener feed actually ANSWERING?

WHY THIS MODULE EXISTS
-----------------------
`MAIN_APP_WS_URL` (bot_module/config.py) defaults to a hostname that is
RESERVED for a future DNS cutover, not a live service. MEASURED 2026-10-29
from this box:

  * ``wss://screener.depthsight.pro/ws/bot/`` -> HTTP 525 (Cloudflare: origin
    unreachable) on both a plain GET and a WebSocket upgrade handshake.
  * ``wss://depthsight.diverseinc.net/ws/bot/`` -> HTTP 403 on a WebSocket
    upgrade handshake (404 on a plain GET): the API registers only ``/ws``
    (api/websocket_server.py:221) and ``/ws/main`` (api/websocket_server.py:390),
    both of which take a ``token`` Query parameter the bot does not send.

Everything else in the repo could only ever answer "is this feed CONFIGURED?".
bot_module/regime_feed.py:111-114 says so in as many words: "This is a
CONFIGURATION test, not a reachability test." A configured-but-DEAD feed was
therefore indistinguishable from a live one -- and the bot's own comment
(bot_module/config.py:470-475) names the consequence: DYNAMIC strategies
"start, appear 'running', and silently never trade".

This module answers the REACHABILITY question with a bounded probe:

  1. DNS resolution -- under its OWN wall-clock budget, because
     `getaddrinfo` does not honour a socket timeout and would otherwise run
     for the resolver's own 10-20s
  2. TCP connect
  3. TLS handshake (wss:// only)
  4. WebSocket upgrade handshake -- so an HTTP status is captured verbatim
     (that is how the 525 above is named)

"Reachable" means a COMPLIANT WebSocket upgrade: HTTP 101, `Upgrade:
websocket`, and a `Sec-WebSocket-Accept` that matches the key this probe
sent. A bare 101 without it is not a feed the bot could trade on, so it is
reported as unreachable.

The WHOLE probe is capped by its timeout (default 2.5s, hard-capped), and
the async form is capped again by `asyncio.wait_for` -- so a health check
can never stall the bot's trading loops.

The result is CACHED for a short window, so a caller on a per-tick loop never
re-probes, and every caller inside the window gets the same answer.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not change the configured URL, and it does not open a server-side
feed. It reports the truth about the URL that IS configured. The feed is
empty by design right now (every published backtest is FAILED), so pointing
the bot at a different URL would only trade one dead feed for another.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import logging
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from bot_module.regime_feed import (
    arms_breakeven_on_regime_change,
    screener_feed_can_deliver_regime_data,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FeedReachability",
    "DEFAULT_SYMBOL_SELECTION_MODE",
    "deployment_uses_main_app_feed",
    "get_main_app_feed_status",
    "get_main_app_feed_status_async",
    "main_app_feed_degraded_reason",
    "main_app_feed_health_payload",
    "origin_header_for_url",
    "probe_main_app_feed",
    "report_main_app_feed_unreachable_once",
    "reset_main_app_feed_cache",
    "reset_startup_report",
    "resolve_symbol_selection_mode",
    "strategy_depends_on_main_app_feed",
    "unknown_feed_health_payload",
]


# Bounded probe. Deliberately far shorter than the bot's own 10s
# `open_timeout` (bot_module/data_consumer.py): a probe must never be able to
# stall the bot, and it must not steal time from the reconnect loop.
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.5

# The upper cap on the probe's wall-clock budget. The budget is a HEALTH
# CHECK: an operator who sets MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS=60 in the
# environment must not be able to turn it into a 60-second stall. The
# configured value is therefore capped, and the cap is what makes the
# "the whole probe is capped by timeout" claim true.
MAX_PROBE_TIMEOUT_SECONDS = 5.0

# The async wrapper's own backstop, on top of the probe's internal deadline.
# The probe already finishes inside its budget; this is the belt to the
# probe's braces, and it is what lets the async form promise a bounded
# return even if the probe's own deadline were ever broken.
ASYNC_PROBE_GRACE_SECONDS = 1.0

# RFC 6455: the server answers a WebSocket upgrade with
# base64(sha1(client_key + GUID)) in `Sec-WebSocket-Accept`. A host that
# answers `101` without it has NOT accepted a WebSocket connection, so it
# is not a feed this bot could trade on.
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# DNS may use at most this share of the probe's budget. The rest is for
# TCP, TLS and the HTTP upgrade: a resolver that is merely slow must not
# eat the whole budget and hide a working host.
DNS_BUDGET_FRACTION = 0.5

# THE ONE default for a missing `symbol_selection_mode`.
#
# It MUST be the same default the strategy resolver uses
# (controller._resolve_symbol_pool_for_strategy): a config with no
# `symbol_selection_mode` TRADES as DYNAMIC, so it needs the screener feed.
# The three copies of this default that disagreed (the feed predicate, the
# resolver, and the published payload) are the reason a feed-dependent
# strategy could be published as a healthy "running" one. The API's own
# schema agrees (api/schemas.py: `symbol_selection_mode: str = "DYNAMIC"`).
DEFAULT_SYMBOL_SELECTION_MODE = "DYNAMIC"

# Cache window. The state publisher runs on a ~2s loop, so without a cache
# the probe would fire ~30 times a minute against a dead host.
DEFAULT_PROBE_CACHE_SECONDS = 60.0

# The probe identifies itself honestly. It is a bot, not a browser; the
# browser-masking User-Agent belongs to the real consumer
# (bot_module/data_consumer.py), and pretending here would make the probe's
# answer unrepresentative of the consumer's.
PROBE_USER_AGENT = "DepthSightBot/1.0 (+reachability-probe)"


@dataclass(frozen=True)
class FeedReachability:
    """The outcome of ONE bounded probe of the configured MAIN_APP feed."""

    url: str
    reachable: bool
    failure: Optional[str]
    status_code: Optional[int]
    detail: str
    checked_at: str
    elapsed_ms: int

    @property
    def failure_label(self) -> str:
        """Short classification, e.g. 'http 525', 'dns', 'timeout'."""
        if self.failure is None:
            return "unknown"
        if self.status_code is not None:
            return f"{self.failure} {self.status_code}"
        return self.failure

    @property
    def reason(self) -> str:
        """Human-readable reason, naming the URL and the actual failure.

        Format (stable, asserted by the tests):

            MAIN_APP feed unreachable: <url> (<failure label>): <detail>
        """
        if self.reachable:
            return ""
        return (
            f"MAIN_APP feed unreachable: {self.url} "
            f"({self.failure_label}): {self.detail}"
        )

    def to_payload(self) -> Dict[str, Any]:
        """JSON-serialisable form, published to Redis and served by the API."""
        return {
            "state": "ok" if self.reachable else "degraded",
            "url": self.url,
            "reachable": self.reachable,
            "failure": self.failure,
            "status_code": self.status_code,
            "detail": self.detail,
            "reason": self.reason or None,
            "checked_at": self.checked_at,
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------
# Configuration resolution (read at CALL time so tests can monkeypatch)
# --------------------------------------------------------------------------


def _bot_config() -> Any:
    try:
        from bot_module import config as bot_config
    except ImportError:  # pragma: no cover - the bot package is always present
        return None
    return bot_config


def _configured_url(url: Optional[str] = None) -> str:
    if url is not None:
        return str(url or "")
    bot_config = _bot_config()
    if bot_config is None:  # pragma: no cover - defensive
        return ""
    return str(getattr(bot_config, "MAIN_APP_WS_URL", "") or "")


def _cap_timeout(value: float) -> float:
    """Clamp a timeout into [50ms, MAX_PROBE_TIMEOUT_SECONDS]."""
    return min(MAX_PROBE_TIMEOUT_SECONDS, max(0.05, float(value)))


def _configured_timeout(explicit: Optional[float] = None) -> float:
    """The probe's wall-clock budget, in seconds.

    CAPPED at `MAX_PROBE_TIMEOUT_SECONDS`. The uncapped `max(0.05, raw)`
    meant an operator could set MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS=60 and
    turn a health check into a 60-second stall -- which is exactly what the
    "the probe can never stall the bot" property forbids.
    """
    if explicit is not None:
        try:
            return _cap_timeout(float(explicit))
        except (TypeError, ValueError):
            return DEFAULT_PROBE_TIMEOUT_SECONDS
    bot_config = _bot_config()
    raw = DEFAULT_PROBE_TIMEOUT_SECONDS
    if bot_config is not None:
        raw = getattr(
            bot_config, "MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS", DEFAULT_PROBE_TIMEOUT_SECONDS
        )
    try:
        return _cap_timeout(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_PROBE_TIMEOUT_SECONDS


def _cache_ttl() -> float:
    bot_config = _bot_config()
    raw = DEFAULT_PROBE_CACHE_SECONDS
    if bot_config is not None:
        raw = getattr(
            bot_config, "MAIN_APP_FEED_PROBE_CACHE_SECONDS", DEFAULT_PROBE_CACHE_SECONDS
        )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_PROBE_CACHE_SECONDS


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------


def origin_header_for_url(url: str) -> str:
    """The Origin header that matches the host we ACTUALLY connect to.

    The consumer used to hardcode ``https://screener.depthsight.pro`` while
    connecting to whatever ``MAIN_APP_WS_URL`` said. For the configured
    default that is the same host, so this returns exactly the same value
    (``https://screener.depthsight.pro``) -- no behaviour change. But when
    the deployment points at a different feed, the Origin follows it instead
    of claiming to be a different site.
    """
    parsed = urlsplit(str(url or ""))
    host = parsed.hostname or ""
    if not host:
        return ""
    scheme = "https" if parsed.scheme.lower() == "wss" else "http"
    default_port = 443 if scheme == "https" else 80
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and port != default_port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


class _ProbeStageError(Exception):
    """An internal, already-classified probe failure."""

    def __init__(
        self, failure: str, detail: str, status_code: Optional[int] = None
    ) -> None:
        super().__init__(detail)
        self.failure = failure
        self.detail = detail
        self.status_code = status_code


def _remaining(deadline: float) -> float:
    """Seconds left before `deadline`, never less than 50ms."""
    return max(0.05, deadline - time.monotonic())


def _resolve_addresses(host: str, port: int, budget: float) -> List[Any]:
    """Resolve `host` to connectable addresses UNDER A WALL-CLOCK BUDGET.

    `socket.getaddrinfo` does NOT honour a socket timeout: it blocks inside
    the resolver, and the socket timeout only applies once a socket exists.
    So `socket.create_connection((host, port), timeout=2.5)` against a
    blackholing resolver ran for the RESOLVER's own timeout (10-20s), not
    2.5s -- the probe was not bounded by its own timeout, which is what the
    docstring claimed.

    The resolution therefore runs in its own daemon thread and the caller
    waits at most `budget` seconds for it. The thread is a daemon, so a
    resolver that never returns cannot keep the process alive.
    """
    outcome: Dict[str, Any] = {}

    def _resolve() -> None:
        try:
            outcome["addresses"] = socket.getaddrinfo(
                host, port, 0, socket.SOCK_STREAM
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            outcome["error"] = exc

    worker = threading.Thread(
        target=_resolve, name="main-app-feed-dns", daemon=True
    )
    worker.start()
    worker.join(timeout=max(0.05, float(budget)))
    if worker.is_alive():
        raise _ProbeStageError(
            "timeout",
            f"DNS resolution of {host} did not finish within {budget:g}s",
        )
    error = outcome.get("error")
    if error is not None:
        raise _ProbeStageError(
            "dns", f"{error.__class__.__name__}: {error}".strip()
        )
    addresses = outcome.get("addresses") or []
    if not addresses:
        raise _ProbeStageError("dns", f"no address was found for {host}")
    return list(addresses)


def _connect(addresses: List[Any], timeout: float) -> socket.socket:
    """Connect to the first address that accepts us."""
    last_error: Optional[BaseException] = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except TimeoutError:
            sock.close()
            raise _ProbeStageError(
                "timeout", f"no TCP connection within {timeout:g}s"
            )
        except OSError as exc:
            sock.close()
            last_error = exc
    raise _ProbeStageError(
        "tcp",
        f"{last_error.__class__.__name__}: {last_error}"
        if last_error is not None
        else "no address could be connected",
    )


def _parse_headers(head: str) -> Dict[str, str]:
    """The response headers of an HTTP response, lower-cased by name."""
    headers: Dict[str, str] = {}
    for line in head.split("\r\n")[1:]:
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        headers[name.strip().lower()] = value.strip()
    return headers


def _expected_ws_accept(key: str) -> str:
    """The `Sec-WebSocket-Accept` a compliant server must answer (RFC 6455)."""
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def probe_main_app_feed(
    url: str, timeout: Optional[float] = None
) -> FeedReachability:
    """Probe ONE URL and report whether the host accepts a WebSocket feed.

    WHAT "REACHABLE" MEANS, EXACTLY
    -------------------------------
    "Reachable" means: the host completed a COMPLIANT WebSocket upgrade
    handshake -- HTTP 101, `Upgrade: websocket`, and a `Sec-WebSocket-Accept`
    that matches the key this probe sent (RFC 6455). A bare `101` without a
    correct accept header is NOT a working feed: the server never agreed to
    a WebSocket, so it is reported as unreachable.

    BOUNDED: the whole probe -- DNS, TCP, TLS and the HTTP upgrade -- is
    capped by `timeout` (default 2.5s, hard-capped at
    `MAX_PROBE_TIMEOUT_SECONDS`), which is deliberately far below the 10s
    `open_timeout` the real consumer uses. DNS gets its own wall-clock
    budget because `getaddrinfo` does not honour a socket timeout.

    Never raises. A probe that throws would be indistinguishable from a
    crash, and a crash in a health check is worse than an unhealthy answer.
    Every failure path -- including a malformed URL that makes `urlsplit`
    raise -- returns a `FeedReachability` with `reachable=False`.
    """
    started = time.monotonic()
    checked_at = datetime.now(timezone.utc).isoformat()
    raw_url = str(url or "")
    timeout_s = _configured_timeout(timeout)

    def _fail(
        failure: str, detail: str, status_code: Optional[int] = None
    ) -> FeedReachability:
        return FeedReachability(
            url=raw_url,
            reachable=False,
            failure=failure,
            status_code=status_code,
            detail=detail,
            checked_at=checked_at,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    sock: Optional[socket.socket] = None
    try:
        # `urlsplit` RAISES on a malformed IPv6 authority ("wss://[::1"),
        # so it is inside the try: the contract above is "never raises".
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss") or not parsed.hostname:
            return _fail("invalid_url", "not a ws:// or wss:// URL with a host")

        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return _fail("invalid_url", "port is not a number")
        if not port:
            port = 443 if scheme == "wss" else 80

        request_target = parsed.path or "/"
        if parsed.query:
            request_target = f"{request_target}?{parsed.query}"
        host_header = host if port in (80, 443) else f"{host}:{port}"

        deadline = time.monotonic() + timeout_s
        try:
            # --- 1. DNS (under its own wall-clock budget) ---------------
            dns_budget = max(
                0.05, min(_remaining(deadline), timeout_s * DNS_BUDGET_FRACTION)
            )
            addresses = _resolve_addresses(host, port, dns_budget)

            # --- 2. TCP --------------------------------------------------
            sock = _connect(addresses, _remaining(deadline))
            sock.settimeout(_remaining(deadline))

            # --- 3. TLS ---------------------------------------------------
            if scheme == "wss":
                context = ssl.create_default_context()
                try:
                    sock = context.wrap_socket(sock, server_hostname=host)
                except TimeoutError:
                    return _fail(
                        "timeout", f"TLS handshake did not finish in {timeout_s:g}s"
                    )
                except ssl.SSLError as exc:
                    return _fail("tls", f"{exc.__class__.__name__}: {exc}")
                except OSError as exc:
                    return _fail("tls", f"{exc.__class__.__name__}: {exc}")
                sock.settimeout(_remaining(deadline))

            # --- 4. WebSocket upgrade handshake ---------------------------
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {request_target} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Origin: {origin_header_for_url(raw_url)}\r\n"
                f"User-Agent: {PROBE_USER_AGENT}\r\n"
                "\r\n"
            )
            buffer = b""
            try:
                sock.sendall(request.encode("ascii", "replace"))
                while b"\r\n\r\n" not in buffer:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > 16384:
                        break
            except TimeoutError:
                return _fail("timeout", f"no HTTP response within {timeout_s:g}s")
            except OSError as exc:
                return _fail("handshake", f"{exc.__class__.__name__}: {exc}")

            if not buffer:
                return _fail(
                    "handshake",
                    "connection closed before an HTTP response was received",
                )

            head = buffer.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
            status_line = head.split("\r\n", 1)[0]
            parts = status_line.split(" ", 2)
            status_code: Optional[int] = None
            if len(parts) >= 2 and parts[1].isdigit():
                status_code = int(parts[1])

            if status_code == 101:
                headers = _parse_headers(head)
                expected_accept = _expected_ws_accept(key)
                accept = headers.get("sec-websocket-accept")
                upgrade = (headers.get("upgrade") or "").lower()
                if accept != expected_accept:
                    return _fail(
                        "handshake",
                        "HTTP 101 without a valid Sec-WebSocket-Accept "
                        f"(expected {expected_accept!r}, got {accept!r})",
                        status_code=101,
                    )
                if "websocket" not in upgrade:
                    return _fail(
                        "handshake",
                        "HTTP 101 without an 'Upgrade: websocket' header "
                        f"(got {upgrade!r})",
                        status_code=101,
                    )
                return FeedReachability(
                    url=raw_url,
                    reachable=True,
                    failure=None,
                    status_code=101,
                    detail="WebSocket upgrade accepted",
                    checked_at=checked_at,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            if status_code is None:
                return _fail(
                    "handshake", f"unparseable HTTP status line: {status_line!r}"
                )
            return _fail(
                "http",
                f"WebSocket upgrade rejected with HTTP status {status_code}",
                status_code=status_code,
            )
        except _ProbeStageError as exc:
            return _fail(exc.failure, exc.detail, exc.status_code)
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
        return _fail("probe_error", f"{exc.__class__.__name__}: {exc}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover - best effort cleanup
                pass


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_PROBE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, FeedReachability]] = {}


def reset_main_app_feed_cache() -> None:
    """Drop every cached probe result (tests, and the rare operator action)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cached(url: str, ttl: float, force_refresh: bool) -> Optional[FeedReachability]:
    if force_refresh:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(url)
        if entry is None:
            return None
        stamp, result = entry
        if (now - stamp) >= ttl:
            return None
        return result


def _store(url: str, result: FeedReachability) -> None:
    with _CACHE_LOCK:
        _CACHE[url] = (time.monotonic(), result)


def get_main_app_feed_status(
    url: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    force_refresh: bool = False,
) -> FeedReachability:
    """Cached reachability of the configured MAIN_APP feed (blocking form).

    Safe to call from a thread. NEVER call it on the event loop: use
    `get_main_app_feed_status_async`, which offloads the probe to a worker
    thread so the bot's loops are never stalled.
    """
    target = _configured_url(url)
    ttl = _cache_ttl()
    hit = _cached(target, ttl, force_refresh)
    if hit is not None:
        return hit
    with _PROBE_LOCK:
        hit = _cached(target, ttl, force_refresh)
        if hit is not None:
            return hit
        result = probe_main_app_feed(target, timeout=timeout)
        _store(target, result)
        return result


async def get_main_app_feed_status_async(
    url: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    force_refresh: bool = False,
) -> FeedReachability:
    """Cached reachability of the configured MAIN_APP feed (awaitable form).

    The probe runs in the default executor, so the event loop is NEVER
    blocked by it -- the bot's trading loops cannot be stalled by a health
    check, which is the whole point of the short timeout.

    BOUNDED: the executor call is wrapped in `asyncio.wait_for`, so this
    coroutine ALWAYS returns within the probe's budget (plus a small grace)
    even if the probe's own deadline were broken. A timeout here is a
    FAILED verdict, not an exception: a health check that raises is worse
    than a health check that says "unreachable".
    """
    target = _configured_url(url)
    ttl = _cache_ttl()
    hit = _cached(target, ttl, force_refresh)
    if hit is not None:
        return hit
    loop = asyncio.get_running_loop()
    budget = _configured_timeout(timeout)
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, functools.partial(probe_main_app_feed, target, timeout)
            ),
            timeout=budget + ASYNC_PROBE_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        result = FeedReachability(
            url=target,
            reachable=False,
            failure="timeout",
            status_code=None,
            detail=(
                f"the probe did not finish within "
                f"{budget + ASYNC_PROBE_GRACE_SECONDS:g}s"
            ),
            checked_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=int(budget * 1000),
        )
    _store(target, result)
    return result


def main_app_feed_health_payload(
    url: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """The JSON payload published with every running strategy."""
    return get_main_app_feed_status(
        url, timeout=timeout, force_refresh=force_refresh
    ).to_payload()


def main_app_feed_degraded_reason(
    url: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    force_refresh: bool = False,
) -> Optional[str]:
    """The reason string when the feed is NOT answering, else None."""
    result = get_main_app_feed_status(
        url, timeout=timeout, force_refresh=force_refresh
    )
    return None if result.reachable else result.reason


def resolve_symbol_selection_mode(config_data: Any) -> str:
    """The ONE authoritative reading of `symbol_selection_mode`.

    A missing (or empty) key means DYNAMIC -- the same default the
    strategy resolver uses (controller._resolve_symbol_pool_for_strategy)
    and the same default the API's own schema uses
    (api/schemas.py: `symbol_selection_mode: str = "DYNAMIC"`).

    This function is the single place that default lives. The three copies
    that disagreed (this module's predicate, the controller's resolver, and
    the controller's published payload) are why a config with no
    `symbol_selection_mode` could TRADE as DYNAMIC -- needing the dead
    feed -- and still be PUBLISHED as a healthy "running" one.

    The raw value is returned (case preserved) so this can be used for both
    the published field and the internal comparisons; callers that compare
    modes case-insensitively should `.upper()` it.
    """
    raw = ""
    if isinstance(config_data, dict):
        raw = config_data.get("symbol_selection_mode") or ""
    elif config_data is not None:
        raw = getattr(config_data, "symbol_selection_mode", "") or ""
    return str(raw).strip() or DEFAULT_SYMBOL_SELECTION_MODE


def deployment_uses_main_app_feed() -> bool:
    """True when THIS deployment is configured to use the MAIN_APP feed.

    A deployment that does NOT use the feed must not be judged by it: the
    probe would answer a question about a URL this deployment never opens,
    and a verdict about it would be a verdict about nothing.

    The condition set is the regime guard's own
    (bot_module/regime_feed.py: `screener_feed_can_deliver_regime_data`):
    SYMBOL_SOURCE_MODE == "MAIN_APP", MAIN_APP_WS_ENABLED, and a
    ws:// / wss:// MAIN_APP_WS_URL. It is reused rather than re-implemented
    so the two can never drift apart.
    """
    return screener_feed_can_deliver_regime_data()


def strategy_depends_on_main_app_feed(
    symbol_selection_mode: Optional[str], config_data: Any = None
) -> bool:
    """True when THIS strategy depends on the MAIN_APP screener feed.

    THE ONE implementation of this question. It used to exist in three
    places with three different defaults for a missing
    `symbol_selection_mode`, which is how a feed-dependent strategy ended
    up published as a healthy "running" one.

    Two dependencies exist, and they are the same two the launch guards
    already know about (api/routes/strategies.py, bot_module/controller.py):

      * DYNAMIC symbol selection -- the feed is the only source of symbols.
      * `breakeven_on_regime_change` -- the feed is the only source of the
        regime change that fires that stop.

    A strategy that depends on NEITHER (a pinned-symbol STATIC/FIXED
    strategy with no regime stop) keeps trading on its own symbols while the
    feed is down, so it is NOT degraded. Marking it degraded would be a
    lie of its own -- a different lie, but a lie.

    A deployment that does not use the MAIN_APP feed at all depends on
    nothing here: the feed is not part of its trading, so its health is
    not part of its truth.
    """
    if not deployment_uses_main_app_feed():
        return False
    mode = str(symbol_selection_mode or DEFAULT_SYMBOL_SELECTION_MODE).strip().upper()
    if mode == "DYNAMIC":
        return True
    return bool(arms_breakeven_on_regime_change(config_data, None))


def unknown_feed_health_payload(
    url: str, failure: str, detail: str
) -> Dict[str, Any]:
    """The payload for "the probe could not be completed".

    Same shape as `FeedReachability.to_payload()`, so a consumer never has
    to special-case it. `state` is "unknown" -- NOT "ok" and NOT
    "degraded": the feed may well be alive, but we could not find out, and
    reporting it as healthy is the lie this module exists to remove.

    The reason names the URL and the failure, exactly like
    `FeedReachability.reason` does for a verdict we DID get.
    """
    target = str(url or "")
    return {
        "state": "unknown",
        "url": target,
        "reachable": False,
        "failure": str(failure or "probe_error"),
        "status_code": None,
        "detail": str(detail or ""),
        "reason": (
            f"MAIN_APP feed reachability is UNKNOWN: {target} "
            f"({failure or 'probe_error'}): {detail}"
        ),
        "checked_at": None,
        "elapsed_ms": None,
    }


# --------------------------------------------------------------------------
# Startup reporting (once per process)
# --------------------------------------------------------------------------

_STARTUP_LOCK = threading.Lock()
_STARTUP_REPORTED = False


def reset_startup_report() -> None:
    """Allow the startup message to be emitted again (tests only)."""
    global _STARTUP_REPORTED
    with _STARTUP_LOCK:
        _STARTUP_REPORTED = False


def report_main_app_feed_unreachable_once(status: FeedReachability) -> bool:
    """Say ONCE, loudly, that the configured feed is not answering.

    Returns True when the message was emitted by this call. A deployment
    starts several DataConsumers (one per account), so the latch keeps this
    to one clear line per process instead of one per account.

    The message names the URL and the failure and carries NO secret value.
    """
    global _STARTUP_REPORTED
    if status.reachable:
        return False
    with _STARTUP_LOCK:
        if _STARTUP_REPORTED:
            return False
        _STARTUP_REPORTED = True
    logger.critical(
        "MAIN_APP SCREENER FEED IS UNREACHABLE: %s (%s): %s. This deployment "
        "is configured to take its symbol list AND its regime-change stop "
        "from that feed, and it is not answering. Every strategy that "
        "depends on it is published as status='degraded' with this reason "
        "instead of status='running', so a dead feed can no longer look "
        "healthy. Point MAIN_APP_WS_URL at a feed that answers, or set "
        "SYMBOL_SOURCE_MODE='STATIC_LIST' deliberately.",
        status.url,
        status.failure_label,
        status.detail,
    )
    return True
