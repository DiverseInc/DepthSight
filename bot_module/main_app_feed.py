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

  1. DNS resolution
  2. TCP connect
  3. TLS handshake (wss:// only)
  4. WebSocket upgrade handshake -- so an HTTP status is captured verbatim
     (that is how the 525 above is named)

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
import logging
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from bot_module.regime_feed import arms_breakeven_on_regime_change

logger = logging.getLogger(__name__)

__all__ = [
    "FeedReachability",
    "get_main_app_feed_status",
    "get_main_app_feed_status_async",
    "main_app_feed_degraded_reason",
    "main_app_feed_health_payload",
    "origin_header_for_url",
    "probe_main_app_feed",
    "report_main_app_feed_unreachable_once",
    "reset_main_app_feed_cache",
    "reset_startup_report",
    "strategy_depends_on_main_app_feed",
]


# Bounded probe. Deliberately far shorter than the bot's own 10s
# `open_timeout` (bot_module/data_consumer.py): a probe must never be able to
# stall the bot, and it must not steal time from the reconnect loop.
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.5

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


def _configured_timeout(explicit: Optional[float] = None) -> float:
    if explicit is not None:
        try:
            return max(0.05, float(explicit))
        except (TypeError, ValueError):
            return DEFAULT_PROBE_TIMEOUT_SECONDS
    bot_config = _bot_config()
    raw = DEFAULT_PROBE_TIMEOUT_SECONDS
    if bot_config is not None:
        raw = getattr(
            bot_config, "MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS", DEFAULT_PROBE_TIMEOUT_SECONDS
        )
    try:
        return max(0.05, float(raw))
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


def probe_main_app_feed(
    url: str, timeout: Optional[float] = None
) -> FeedReachability:
    """Probe ONE URL and report whether the host answers a WS handshake.

    Bounded: the whole probe -- DNS, TCP, TLS and the HTTP upgrade -- is
    capped by `timeout` (default 2.5s), which is deliberately far below the
    10s `open_timeout` the real consumer uses.

    Never raises. A probe that throws would be indistinguishable from a
    crash, and a crash in a health check is worse than an unhealthy answer.
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
    sock: Optional[socket.socket] = None
    try:
        # --- 1. DNS + TCP -------------------------------------------------
        try:
            sock = socket.create_connection((host, port), timeout=timeout_s)
        except socket.gaierror as exc:
            return _fail("dns", f"{exc.__class__.__name__}: {exc}".strip())
        except TimeoutError:
            return _fail("timeout", f"no TCP connection within {timeout_s:g}s")
        except OSError as exc:
            return _fail("tcp", f"{exc.__class__.__name__}: {exc}")

        sock.settimeout(max(0.05, deadline - time.monotonic()))

        # --- 2. TLS -------------------------------------------------------
        if scheme == "wss":
            context = ssl.create_default_context()
            try:
                sock = context.wrap_socket(sock, server_hostname=host)
            except TimeoutError:
                return _fail("timeout", f"TLS handshake did not finish in {timeout_s:g}s")
            except ssl.SSLError as exc:
                return _fail("tls", f"{exc.__class__.__name__}: {exc}")
            except OSError as exc:
                return _fail("tls", f"{exc.__class__.__name__}: {exc}")
            sock.settimeout(max(0.05, deadline - time.monotonic()))

        # --- 3. WebSocket upgrade handshake -------------------------------
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
                "handshake", "connection closed before an HTTP response was received"
            )

        status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = status_line.split(" ", 2)
        status_code: Optional[int] = None
        if len(parts) >= 2 and parts[1].isdigit():
            status_code = int(parts[1])

        if status_code == 101:
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
    """
    target = _configured_url(url)
    ttl = _cache_ttl()
    hit = _cached(target, ttl, force_refresh)
    if hit is not None:
        return hit
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, functools.partial(probe_main_app_feed, target, timeout)
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


def strategy_depends_on_main_app_feed(
    symbol_selection_mode: Optional[str], config_data: Any = None
) -> bool:
    """True when THIS strategy depends on the MAIN_APP screener feed.

    Two dependencies exist, and they are the same two the launch guards
    already know about (api/routes/strategies.py, bot_module/controller.py):

      * DYNAMIC symbol selection -- the feed is the only source of symbols.
      * `breakeven_on_regime_change` -- the feed is the only source of the
        regime change that fires that stop.

    A strategy that depends on NEITHER (a pinned-symbol STATIC/FIXED
    strategy with no regime stop) keeps trading on its own symbols while the
    feed is down, so it is NOT degraded. Marking it degraded would be a
    lie of its own -- a different lie, but a lie.
    """
    if str(symbol_selection_mode or "").strip().upper() == "DYNAMIC":
        return True
    return bool(arms_breakeven_on_regime_change(config_data, None))


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
