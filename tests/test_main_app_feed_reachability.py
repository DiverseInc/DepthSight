# File: tests/test_main_app_feed_reachability.py
"""The MAIN_APP screener feed must be MEASURED, not assumed.

THE DEFECT
----------
`MAIN_APP_WS_URL` (bot_module/config.py) defaults to
`wss://screener.depthsight.pro/ws/bot/`. MEASURED 2026-10-29 from this box:

  * `wss://screener.depthsight.pro/ws/bot/` -> HTTP 525 on a WebSocket
    upgrade handshake (and on every plain path).
  * `wss://depthsight.diverseinc.net/ws/bot/` -> HTTP 403: the API registers
    only `/ws` and `/ws/main`, and Starlette answers 403 for ANY unmatched
    WebSocket path.

So the shipped default cannot connect. Everything in the repo could only ever
answer "is this feed CONFIGURED?" -- bot_module/regime_feed.py:111-114 says so
in as many words -- and the bot's own comment (bot_module/config.py:470-475)
names the consequence: DYNAMIC strategies "start, appear 'running', and
silently never trade".

THE FIX, AND WHAT EACH TEST PINS
--------------------------------
  1. A bounded probe really measures reachability
     (dns / tcp / tls / http <status> / timeout).
  2. A timeout CANNOT hang the bot.
  3. The degraded reason NAMES the URL and the failure.
  4. The bot does NOT report a healthy "running" strategy while the feed it
     depends on is dead -- it reports status='degraded' with the reason.
  5. The probe result is CACHED, so a per-tick caller never re-probes.
  6. The API carries the degraded state additively, without breaking the
     existing response shape.

These tests use a REAL local TCP server for the reachable / HTTP-rejected /
timeout cases. A mocked socket would only prove the mock agrees with itself.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import threading
import time
import types
from contextlib import contextmanager

import pytest

from bot_module import config as bot_config
from bot_module import main_app_feed
from bot_module.controller import ActivePositionMap, TradingController
from bot_module.main_app_feed import (
    FeedReachability,
    get_main_app_feed_status,
    get_main_app_feed_status_async,
    origin_header_for_url,
    probe_main_app_feed,
    report_main_app_feed_unreachable_once,
    reset_main_app_feed_cache,
    reset_startup_report,
    resolve_symbol_selection_mode,
    strategy_depends_on_main_app_feed,
)


# --------------------------------------------------------------------------
# A real local WebSocket-ish server
# --------------------------------------------------------------------------

# RFC 6455: the server answers a WebSocket upgrade with
# base64(sha1(client_key + GUID)) in `Sec-WebSocket-Accept`.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key: str) -> str:
    """The `Sec-WebSocket-Accept` a compliant server must answer."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _with_ws_accept(request: bytes, response: bytes) -> bytes:
    """Add the correct `Sec-WebSocket-Accept` to a 101 response.

    The probe now REQUIRES it: a bare 101 is not a WebSocket handshake, and
    a host that answers one is not a feed this bot could trade on. So the
    test server has to behave like a compliant server.
    """
    if not response.startswith(b"HTTP/1.1 101"):
        return response
    key = ""
    for line in request.decode("latin-1").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "sec-websocket-key":
            key = value.strip()
    if not key:
        return response
    return response.replace(
        b"\r\n\r\n",
        f"\r\nSec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n".encode("ascii"),
        1,
    )


@contextmanager
def _tcp_server(
    response: bytes = b"", respond_delay: float = 0.0, valid_ws_accept: bool = True
):
    """A real TCP server on 127.0.0.1 that answers with `response`.

    `response=b""` + `respond_delay>0` models a host that ACCEPTS the
    connection and then says nothing -- the timeout case.

    `valid_ws_accept=False` models a host that answers a bare HTTP 101
    without a `Sec-WebSocket-Accept` -- NOT a WebSocket handshake.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    host, port = server.getsockname()
    stop = threading.Event()

    def _serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.settimeout(2.0)
                request = b""
                try:
                    request = conn.recv(4096)
                    while b"\r\n\r\n" not in request and len(request) < 16384:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        request += chunk
                except OSError:
                    pass
                if respond_delay:
                    stop.wait(respond_delay)
                if response:
                    payload = response
                    if valid_ws_accept:
                        payload = _with_ws_accept(request, payload)
                    conn.sendall(payload)
                time.sleep(0.05)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield f"ws://{host}:{port}/ws/bot/"
    finally:
        stop.set()
        thread.join(timeout=3)
        try:
            server.close()
        except OSError:
            pass


@contextmanager
def _closed_port():
    """A URL whose port nothing is listening on (connection refused)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    host, port = server.getsockname()
    server.close()
    yield f"ws://{host}:{port}/ws/bot/"


ACCEPTED = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"\r\n"
)
REJECTED_525 = b"HTTP/1.1 525 SSL Handshake Failed\r\nServer: cloudflare\r\n\r\n"


# --------------------------------------------------------------------------
# 1. The probe measures reachability
# --------------------------------------------------------------------------


def test_a_reachable_feed_is_detected():
    """A host that accepts the WebSocket upgrade IS reachable."""
    with _tcp_server(ACCEPTED) as url:
        result = probe_main_app_feed(url, timeout=5.0)

    assert result.reachable is True, f"expected reachable, got {result.reason!r}"
    assert result.status_code == 101
    assert result.failure is None
    # A healthy feed has no reason to report.
    assert result.reason == ""
    assert result.to_payload()["state"] == "ok"


def test_an_unreachable_feed_is_detected():
    """Nothing listening -> the probe must say so, not guess."""
    with _closed_port() as url:
        result = probe_main_app_feed(url, timeout=5.0)

    assert result.reachable is False
    assert result.failure in ("tcp", "timeout"), result.failure
    assert result.reason


def test_an_http_rejection_is_reported_with_its_status():
    """The 525 that this product's default host actually returns."""
    with _tcp_server(REJECTED_525) as url:
        result = probe_main_app_feed(url, timeout=5.0)

    assert result.reachable is False
    assert result.failure == "http"
    assert result.status_code == 525
    assert result.failure_label == "http 525"
    assert "525" in result.detail


def test_a_dns_failure_is_classified_as_dns(monkeypatch):
    """A name that does not resolve must not be reported as a TCP failure."""

    def _boom(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    result = probe_main_app_feed("wss://no-such-host.invalid/ws/bot/", timeout=5.0)

    assert result.reachable is False
    assert result.failure == "dns", result.failure
    assert "no-such-host.invalid" in result.reason


def test_a_non_ws_url_is_rejected_without_a_network_call():
    """A bad URL is a configuration error, not an outage."""
    result = probe_main_app_feed("https://screener.depthsight.pro/ws/bot/", timeout=1.0)

    assert result.reachable is False
    assert result.failure == "invalid_url"


def test_a_malformed_url_never_raises():
    """`urlsplit` RAISES on a malformed IPv6 authority.

    The module promises "never raises", and the promise was FALSE: the URL
    parsing sat OUTSIDE the try, so `probe_main_app_feed("wss://[::1")`
    raised ValueError instead of answering. A health check that throws is
    indistinguishable from a crash -- and a crash in a health check is
    worse than an unhealthy answer.
    """
    result = probe_main_app_feed("wss://[::1", timeout=1.0)

    assert result.reachable is False
    assert result.failure, "a failed probe must say WHY it failed"
    assert "wss://[::1" in result.reason
    assert result.reason.startswith("MAIN_APP feed unreachable: ")


def test_a_101_without_a_valid_accept_is_not_reachable():
    """A bare 101 is NOT a WebSocket handshake.

    The probe used to declare "reachable" on any HTTP 101, so a host that
    answered 101 without a correct `Sec-WebSocket-Accept` was reported as a
    working feed. It is not: that server never agreed to a WebSocket, so
    the bot could never trade on it.
    """
    with _tcp_server(ACCEPTED, valid_ws_accept=False) as url:
        result = probe_main_app_feed(url, timeout=5.0)

    assert result.reachable is False
    assert result.failure == "handshake"
    assert "Sec-WebSocket-Accept" in result.detail
    assert url in result.reason


def test_a_101_without_the_upgrade_header_is_not_reachable():
    """The 101 must also say `Upgrade: websocket`."""
    response = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Connection: Upgrade\r\n"
        b"\r\n"
    )
    with _tcp_server(response) as url:
        result = probe_main_app_feed(url, timeout=5.0)

    assert result.reachable is False
    assert result.failure == "handshake"
    assert "Upgrade: websocket" in result.detail


# --------------------------------------------------------------------------
# 2. A timeout cannot hang the bot
# --------------------------------------------------------------------------


def test_a_timeout_does_not_hang():
    """The host accepts and then says nothing: the probe must give up.

    This is the property that makes the probe safe to put in the bot's
    startup path: it is bounded, so it cannot stall the bot.
    """
    with _tcp_server(b"", respond_delay=30.0) as url:
        started = time.monotonic()
        result = probe_main_app_feed(url, timeout=0.4)
        elapsed = time.monotonic() - started

    assert result.reachable is False
    assert result.failure == "timeout", result.failure
    assert elapsed < 5.0, f"the probe hung for {elapsed:.1f}s"


def test_the_configured_probe_timeout_is_short():
    """The probe must be far below the 10s open_timeout the consumer uses.

    A health check that can stall the trading loop is not a health check.
    """
    assert bot_config.MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS <= 3.0
    assert bot_config.MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS < 10.0


def test_the_configured_probe_timeout_is_capped(monkeypatch):
    """An operator must not be able to turn a health check into a stall.

    The budget used to be `max(0.05, raw)` with NO upper bound, so
    MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS=60 turned a health check into a
    60-second stall -- exactly what the property above forbids.
    """
    monkeypatch.setattr(bot_config, "MAIN_APP_FEED_PROBE_TIMEOUT_SECONDS", 600.0)

    assert main_app_feed._configured_timeout() == main_app_feed.MAX_PROBE_TIMEOUT_SECONDS
    assert main_app_feed._configured_timeout(600.0) == main_app_feed.MAX_PROBE_TIMEOUT_SECONDS
    # And a sane value is used as-is.
    assert main_app_feed._configured_timeout(1.5) == 1.5


async def test_the_probe_is_bounded_when_dns_blackholes(monkeypatch):
    """A blackholing resolver must not make the probe run for 10-20s.

    `socket.create_connection` resolves the name BEFORE the socket timeout
    applies, so the DNS step used to run for the resolver's own timeout
    (10-20s) instead of the configured 2.5s -- the docstring's "capped by
    timeout" claim was false. DNS now has its own wall-clock budget.
    """
    reset_main_app_feed_cache()

    def _blackhole(*args, **kwargs):
        time.sleep(30)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", _blackhole)

    started = time.monotonic()
    result = await get_main_app_feed_status_async("wss://blackholed.invalid/ws/bot/")
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"the probe ran for {elapsed:.1f}s"
    assert result.reachable is False
    assert result.failure == "timeout"
    reset_main_app_feed_cache()


# --------------------------------------------------------------------------
# 3. The degraded reason names the URL and the failure
# --------------------------------------------------------------------------


def test_the_degraded_reason_names_the_url_and_the_failure():
    """The reason is what a human reads at 3am. It must be actionable."""
    with _tcp_server(REJECTED_525) as url:
        result = probe_main_app_feed(url, timeout=5.0)

    assert url in result.reason, result.reason
    assert "http 525" in result.reason, result.reason
    assert result.reason.startswith("MAIN_APP feed unreachable: ")

    payload = result.to_payload()
    assert payload["state"] == "degraded"
    assert payload["url"] == url
    assert payload["status_code"] == 525
    assert payload["reason"] == result.reason


def test_the_reason_format_is_stable():
    """Pin the format so it cannot drift between releases."""
    result = FeedReachability(
        url="wss://screener.depthsight.pro/ws/bot/",
        reachable=False,
        failure="http",
        status_code=525,
        detail="WebSocket upgrade rejected with HTTP status 525",
        checked_at="2026-10-29T00:00:00+00:00",
        elapsed_ms=717,
    )
    assert result.reason == (
        "MAIN_APP feed unreachable: wss://screener.depthsight.pro/ws/bot/ "
        "(http 525): WebSocket upgrade rejected with HTTP status 525"
    )


# --------------------------------------------------------------------------
# 4. The bot does not report a healthy strategy while its feed is dead
# --------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, sink):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def set(self, key, value):
        self._sink[key] = value

    def publish(self, channel, message):
        pass

    async def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakePipeline(self.store)


class _FakeRiskManager:
    def __init__(self):
        self.stats = types.SimpleNamespace(
            current_balance=1000.0, today_pnl=0.0, consecutive_losses=0
        )
        self._is_trading_allowed = True

    def get_trading_disabled_reason(self):
        return None


def _build_controller(strategies):
    """A controller carrying only the surface the publisher touches."""
    redis_client = _FakeRedis()
    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = 1
    controller.api_key_id = None
    controller.api_key_name = "paper"
    controller.executors = {}
    controller.market_executors = {}
    controller.consumer = None
    controller.redis_client = redis_client
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = strategies
    controller.rm = _FakeRiskManager()

    async def _no_pnl(config_ids):
        """The PnL aggregate is a database concern, not a feed concern."""
        return None

    controller._fetch_strategy_pnl_aggregates = _no_pnl
    return controller, redis_client


def _strategy_entry(*, mode="STATIC", config_data=None, omit_mode=False):
    instance = types.SimpleNamespace(NAME="VisualBuilderStrategy")
    config_dict = {
        "user_id": 1,
        "mode": "paper",
        "symbol_selection_mode": mode,
        "symbols": ["BTCUSDT"],
        "config_data": config_data if config_data is not None else {"params": {}},
        "started_at": "2026-10-29T00:00:00+00:00",
    }
    if omit_mode:
        # A config written before `symbol_selection_mode` existed, or one
        # that never had it set. It TRADES as DYNAMIC (that is the
        # resolver's default), so it depends on the feed.
        config_dict.pop("symbol_selection_mode")
    return (instance, config_dict)


def _published(redis_client):
    key = f"{bot_config.REDIS_STATE_KEY_STRATEGIES}:1:None"
    import json

    return {str(s["id"]): s for s in json.loads(redis_client.store[key])}


DEAD_FEED = FeedReachability(
    url="wss://screener.depthsight.pro/ws/bot/",
    reachable=False,
    failure="http",
    status_code=525,
    detail="WebSocket upgrade rejected with HTTP status 525",
    checked_at="2026-10-29T00:00:00+00:00",
    elapsed_ms=717,
)

LIVE_FEED = FeedReachability(
    url="wss://screener.depthsight.pro/ws/bot/",
    reachable=True,
    failure=None,
    status_code=101,
    detail="WebSocket upgrade accepted",
    checked_at="2026-10-29T00:00:00+00:00",
    elapsed_ms=120,
)


def _patch_feed(monkeypatch, status):
    async def _fake(*args, **kwargs):
        return status

    monkeypatch.setattr(
        "bot_module.controller.get_main_app_feed_status_async", _fake, raising=True
    )


async def test_the_bot_does_not_report_running_while_its_feed_is_dead(monkeypatch):
    """THE DEFECT: a feed-dependent strategy must not look healthy.

    A DYNAMIC strategy takes its symbols from this feed and nothing else.
    With the feed dead it starts, reports a heartbeat, and never trades --
    while the dashboard says "running".
    """
    _patch_feed(monkeypatch, DEAD_FEED)
    strategies = {"cfg-dynamic": _strategy_entry(mode="DYNAMIC")}
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-dynamic"]

    assert published["status"] != "running", published
    assert published["status"] == "degraded", published
    assert "screener.depthsight.pro" in published["status_detail"]
    assert "525" in published["status_detail"]


async def test_a_strategy_arming_the_regime_stop_is_degraded_too(monkeypatch):
    """The regime-change stop is fed ONLY by this socket.

    With the socket dead the stop is inert from the first second, so the
    strategy must not be published as a working one.
    """
    _patch_feed(monkeypatch, DEAD_FEED)
    strategies = {
        "cfg-regime": _strategy_entry(
            mode="STATIC",
            config_data={"params": {}, "breakeven_on_regime_change": True},
        )
    }
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-regime"]

    assert published["status"] == "degraded", published
    assert "screener.depthsight.pro" in published["status_detail"]


async def test_a_healthy_feed_leaves_the_status_untouched(monkeypatch):
    """The overwhelmingly common case must not change."""
    _patch_feed(monkeypatch, LIVE_FEED)
    strategies = {"cfg-dynamic": _strategy_entry(mode="DYNAMIC")}
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-dynamic"]

    assert published["status"] == "running", published
    assert published["status_detail"] is None


async def test_a_strategy_that_does_not_depend_on_the_feed_is_not_degraded(
    monkeypatch,
):
    """A pinned-symbol strategy keeps trading on its own symbols.

    The screener feed is a SECONDARY input: candles flow through the
    exchange/redis-market path regardless (vault: "candles flow fine via
    redis-market pub/sub without it"). Marking an unaffected strategy
    degraded would be a lie of its own.
    """
    _patch_feed(monkeypatch, DEAD_FEED)
    strategies = {"cfg-pinned": _strategy_entry(mode="STATIC")}
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-pinned"]

    assert published["status"] == "running", published


async def test_the_published_feed_health_names_the_url(monkeypatch):
    """The API needs the reason, not just a colour."""
    _patch_feed(monkeypatch, DEAD_FEED)
    strategies = {"cfg-dynamic": _strategy_entry(mode="DYNAMIC")}
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-dynamic"]

    health = published["feed_health"]
    assert health is not None
    assert health["state"] == "degraded"
    assert health["url"] == "wss://screener.depthsight.pro/ws/bot/"
    assert health["status_code"] == 525
    assert "screener.depthsight.pro" in health["reason"]


def test_the_status_truth_table():
    """The three states, and their precedence."""
    from bot_module.controller import _derive_strategy_status

    assert _derive_strategy_status(0, None) == "running"
    assert _derive_strategy_status(1, None) == "in_position"
    assert _derive_strategy_status(0, "no entryConditions key") == "cannot_trade"
    assert _derive_strategy_status(1, "no entryConditions key") == "in_position"
    # A dead feed outranks everything: a green badge on an unprotected
    # position is the lie this change exists to remove.
    assert _derive_strategy_status(0, None, "feed down") == "degraded"
    assert _derive_strategy_status(1, None, "feed down") == "degraded"
    assert _derive_strategy_status(0, "no entryConditions key", "feed down") == (
        "degraded"
    )


def test_which_strategies_depend_on_the_feed():
    """Affectedness is per strategy, not global."""
    assert strategy_depends_on_main_app_feed("DYNAMIC", {}) is True
    assert (
        strategy_depends_on_main_app_feed("STATIC", {"breakeven_on_regime_change": True})
        is True
    )
    assert strategy_depends_on_main_app_feed("STATIC", {}) is False
    assert strategy_depends_on_main_app_feed("FIXED", {"params": {}}) is False
    # A MISSING `symbol_selection_mode` means DYNAMIC -- the same default
    # the resolver that decides how the strategy TRADES uses. This
    # assertion used to say False, and that was the defect: the predicate
    # read a missing key as "" (not feed-dependent) while the strategy
    # traded as DYNAMIC (needs the feed).
    assert strategy_depends_on_main_app_feed(None, None) is True


def test_a_missing_symbol_selection_mode_means_dynamic():
    """ONE default, in ONE place, used by everyone.

    The predicate, the strategy resolver and the published payload used to
    disagree about a missing `symbol_selection_mode` ("" vs "DYNAMIC" vs
    "STATIC"). A config without the key therefore TRADES as DYNAMIC -- it
    needs the screener feed -- while being published as a pinned STATIC
    strategy that did not.
    """
    assert resolve_symbol_selection_mode({}) == "DYNAMIC"
    assert resolve_symbol_selection_mode({"symbol_selection_mode": None}) == "DYNAMIC"
    assert resolve_symbol_selection_mode({"symbol_selection_mode": ""}) == "DYNAMIC"
    assert resolve_symbol_selection_mode({"symbol_selection_mode": "STATIC"}) == "STATIC"
    assert resolve_symbol_selection_mode({"symbol_selection_mode": "FIXED"}) == "FIXED"

    # And the predicate agrees with it.
    assert strategy_depends_on_main_app_feed(None, {}) is True
    assert strategy_depends_on_main_app_feed("", {}) is True


async def test_a_config_without_a_symbol_selection_mode_is_degraded(monkeypatch):
    """It TRADES as DYNAMIC, so a dead feed must degrade it."""
    _patch_feed(monkeypatch, DEAD_FEED)
    strategies = {"cfg-no-mode": _strategy_entry(omit_mode=True)}
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-no-mode"]

    assert published["status"] == "degraded", published
    assert "screener.depthsight.pro" in published["status_detail"]
    # And it is PUBLISHED as what it actually is.
    assert published["symbol_selection_mode"] == "DYNAMIC"


async def test_a_probe_failure_is_never_published_as_running(monkeypatch):
    """FAIL CLOSED: no verdict means no "running".

    The old handler caught the probe's exception and left the degraded
    reason EMPTY, so the strategy was published as status="running" with
    feed_health=None -- a dead feed reported healthy, which is the exact
    failure this branch exists to remove.

    The invariant: "running" is only ever published on a POSITIVE
    reachability verdict.
    """

    async def _explode(*args, **kwargs):
        raise RuntimeError("the probe exploded")

    monkeypatch.setattr(
        "bot_module.controller.get_main_app_feed_status_async",
        _explode,
        raising=True,
    )
    strategies = {
        "cfg-dynamic": _strategy_entry(mode="DYNAMIC"),
        "cfg-pinned": _strategy_entry(mode="STATIC"),
    }
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)

    for key in ("cfg-dynamic", "cfg-pinned"):
        assert published[key]["status"] != "running", published[key]
        assert published[key]["status"] == "degraded", published[key]
        assert "UNKNOWN" in published[key]["status_detail"]

    health = published["cfg-dynamic"]["feed_health"]
    assert health is not None
    assert health["state"] == "unknown"
    assert health["url"], "the unknown verdict must name the URL"
    assert "probe_error" in health["reason"]
    assert "the probe exploded" in health["detail"]


async def test_a_deployment_that_does_not_use_the_feed_is_not_judged_by_it(
    monkeypatch,
):
    """No feed, no probe, no verdict, no degradation.

    In a deployment that does not use the MAIN_APP feed, the probe would
    answer a question about a URL this deployment never opens.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    async def _must_not_run(*args, **kwargs):
        raise AssertionError(
            "the probe must not run in a deployment that does not use the feed"
        )

    monkeypatch.setattr(
        "bot_module.controller.get_main_app_feed_status_async",
        _must_not_run,
        raising=True,
    )
    strategies = {"cfg-dynamic": _strategy_entry(mode="DYNAMIC")}
    controller, redis_client = _build_controller(strategies)

    await controller._publish_state_to_redis()
    published = _published(redis_client)["cfg-dynamic"]

    assert published["status"] == "running", published
    assert published["feed_health"] is None


def test_the_predicate_ignores_a_deployment_without_the_feed(monkeypatch):
    """A deployment that does not use the feed depends on nothing here."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")
    assert strategy_depends_on_main_app_feed("DYNAMIC", {}) is False
    assert (
        strategy_depends_on_main_app_feed(
            "STATIC", {"breakeven_on_regime_change": True}
        )
        is False
    )

    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "MAIN_APP")
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", False)
    assert strategy_depends_on_main_app_feed("DYNAMIC", {}) is False


# --------------------------------------------------------------------------
# 5. The probe is cached, not re-run per tick
# --------------------------------------------------------------------------


def test_the_probe_result_is_cached_and_not_re_probed_per_tick(monkeypatch):
    """The publisher runs on a ~2s loop. Probing per tick is a flood."""
    reset_main_app_feed_cache()
    calls = []

    def _counting_probe(url, timeout=None):
        calls.append(url)
        return DEAD_FEED

    monkeypatch.setattr(main_app_feed, "probe_main_app_feed", _counting_probe)
    url = "wss://screener.depthsight.pro/ws/bot/"

    first = get_main_app_feed_status(url)
    for _ in range(25):
        again = get_main_app_feed_status(url)
        assert again is first

    assert len(calls) == 1, f"the probe ran {len(calls)} times for one cache window"
    reset_main_app_feed_cache()


async def test_the_async_probe_is_cached_too(monkeypatch):
    """The async path is the one the bot uses, so it must cache as well."""
    reset_main_app_feed_cache()
    calls = []

    def _counting_probe(url, timeout=None):
        calls.append(url)
        return LIVE_FEED

    monkeypatch.setattr(main_app_feed, "probe_main_app_feed", _counting_probe)
    url = "wss://screener.depthsight.pro/ws/bot/"

    first = await get_main_app_feed_status_async(url)
    for _ in range(25):
        again = await get_main_app_feed_status_async(url)
        assert again is first

    assert len(calls) == 1, f"the probe ran {len(calls)} times for one cache window"
    reset_main_app_feed_cache()


def test_the_cache_expires(monkeypatch):
    """A dead host that comes back must be noticed."""
    reset_main_app_feed_cache()
    calls = []

    def _counting_probe(url, timeout=None):
        calls.append(url)
        return DEAD_FEED

    monkeypatch.setattr(main_app_feed, "probe_main_app_feed", _counting_probe)
    monkeypatch.setattr(bot_config, "MAIN_APP_FEED_PROBE_CACHE_SECONDS", 0.0)
    url = "wss://screener.depthsight.pro/ws/bot/"

    get_main_app_feed_status(url)
    get_main_app_feed_status(url)

    assert len(calls) == 2, "an expired cache must re-probe"
    reset_main_app_feed_cache()


def test_a_reachable_probe_does_not_block_the_event_loop(monkeypatch):
    """The probe runs in a worker thread, so the bot's loop never stalls.

    The tick counter is SHARED with the heartbeat task and the assertion is
    `ticks > 0`. The previous version of this test read `beat.result()`
    from a heartbeat task that NEVER finishes, so `ticks` was always None
    and `assert ticks is None or ticks > 0` was vacuously true: the test
    could not fail, whatever the probe did. That is why the CI gate stayed
    green while the probe was unbounded.
    """
    reset_main_app_feed_cache()

    def _slow_probe(url, timeout=None):
        time.sleep(0.4)
        return LIVE_FEED

    monkeypatch.setattr(main_app_feed, "probe_main_app_feed", _slow_probe)

    ticks = 0

    async def _heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    async def _run():
        beat = asyncio.ensure_future(_heartbeat())
        await get_main_app_feed_status_async("wss://screener.depthsight.pro/ws/bot/")
        observed = ticks
        beat.cancel()
        return observed

    observed = asyncio.new_event_loop().run_until_complete(_run())
    assert observed > 0, "the event loop was blocked by the probe"
    reset_main_app_feed_cache()


# --------------------------------------------------------------------------
# 6. The API carries the degraded state additively
# --------------------------------------------------------------------------


def _published_payload(**overrides):
    """The exact dict shape controller.py writes to Redis."""
    payload = {
        "id": "e898f74e-0000-4fb3-acc1-c4fc46177533",
        "strategy_name": "VisualBuilderStrategy",
        "symbol": "BTCUSDT",
        "market_type": "futures",
        "status": "degraded",
        "status_detail": (
            "MAIN_APP feed unreachable: wss://screener.depthsight.pro/ws/bot/ "
            "(http 525): WebSocket upgrade rejected with HTTP status 525"
        ),
        "feed_health": {
            "state": "degraded",
            "url": "wss://screener.depthsight.pro/ws/bot/",
            "reachable": False,
            "failure": "http",
            "status_code": 525,
            "detail": "WebSocket upgrade rejected with HTTP status 525",
            "reason": (
                "MAIN_APP feed unreachable: wss://screener.depthsight.pro/ws/bot/ "
                "(http 525): WebSocket upgrade rejected with HTTP status 525"
            ),
            "checked_at": "2026-10-29T00:00:00+00:00",
            "elapsed_ms": 717,
        },
        "pnl": 0.0,
        "open_positions": 0,
        "started_at": "2026-10-06T18:00:00+00:00",
        "params": {},
        "user_id": 10,
        "api_key_id": None,
        "symbol_selection_mode": "STATIC",
        "mode": "paper",
        "name": "EMA 50/200 Golden Cross",
    }
    payload.update(overrides)
    return payload


def test_the_api_carries_the_degraded_state_and_its_reason():
    """Without the declared field Pydantic strips it silently."""
    from api import schemas

    info = schemas.StrategyInfo(**_published_payload())
    assert info.status == "degraded"
    assert info.status_detail is not None
    assert "525" in info.status_detail
    assert info.feed_health is not None
    assert info.feed_health["state"] == "degraded"
    assert info.feed_health["status_code"] == 525
    assert "screener.depthsight.pro" in info.feed_health["reason"]


def test_the_api_still_accepts_a_payload_without_the_new_key():
    """A strategy published by an OLDER bot has no `feed_health` key."""
    from api import schemas

    payload = _published_payload(status="running", status_detail=None)
    payload.pop("feed_health")
    info = schemas.StrategyInfo(**payload)
    assert info.status == "running"
    assert info.feed_health is None


def test_the_existing_status_values_are_unchanged():
    """The new field is ADDITIVE: every existing status value still works."""
    from api import schemas

    for status in ("running", "in_position", "cannot_trade"):
        info = schemas.StrategyInfo(**_published_payload(status=status))
        assert info.status == status


async def test_the_feed_health_route_reports_the_degraded_feed(
    authenticated_client, monkeypatch
):
    """The deployment-level answer, for when no strategy is running."""
    from api.routes import strategies as strategies_route

    async def _fake(*args, **kwargs):
        return DEAD_FEED

    monkeypatch.setattr(
        strategies_route, "_get_main_app_feed_status_async", _fake, raising=True
    )

    response = await authenticated_client.get("/api/v1/strategies/feed-health")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["state"] == "degraded"
    assert data["status_code"] == 525
    assert "screener.depthsight.pro" in data["reason"]


async def test_the_feed_health_route_names_the_url_and_the_failure(
    authenticated_client, monkeypatch
):
    """The "unknown" answer must be as actionable as a verdict.

    It used to say only "the probe could not be completed", which names
    neither the feed nor the failure -- so a human reading it learns
    nothing about WHICH feed or WHY.
    """
    from api.routes import strategies as strategies_route

    async def _explode(*args, **kwargs):
        raise RuntimeError("the probe exploded")

    monkeypatch.setattr(
        strategies_route, "_get_main_app_feed_status_async", _explode, raising=True
    )

    response = await authenticated_client.get("/api/v1/strategies/feed-health")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["state"] == "unknown"
    assert data["url"], "the unknown verdict must name the URL"
    assert data["url"] in data["reason"]
    assert "probe_error" in data["reason"]
    assert "the probe exploded" in data["reason"]


# --------------------------------------------------------------------------
# 7. The Origin header and the startup announcement
# --------------------------------------------------------------------------


def test_the_origin_header_matches_the_host_we_connect_to():
    """For the shipped default this is byte-identical to the old value."""
    assert (
        origin_header_for_url("wss://screener.depthsight.pro/ws/bot/")
        == "https://screener.depthsight.pro"
    )
    assert (
        origin_header_for_url("wss://depthsight.diverseinc.net/ws/main")
        == "https://depthsight.diverseinc.net"
    )
    assert origin_header_for_url("ws://127.0.0.1:8765/ws") == "http://127.0.0.1:8765"


def test_the_startup_message_is_emitted_once_per_process(caplog):
    """A deployment starts several DataConsumers; one clear line is enough."""
    reset_startup_report()
    with caplog.at_level("CRITICAL", logger="bot_module.main_app_feed"):
        assert report_main_app_feed_unreachable_once(DEAD_FEED) is True
        assert report_main_app_feed_unreachable_once(DEAD_FEED) is False

    criticals = [
        r for r in caplog.records if r.levelname == "CRITICAL"
    ]
    assert len(criticals) == 1, "the unreachable-feed message must be said once"
    message = criticals[0].getMessage()
    assert "screener.depthsight.pro" in message
    assert "525" in message
    reset_startup_report()


def test_a_healthy_feed_is_never_reported_as_unreachable(caplog):
    reset_startup_report()
    with caplog.at_level("CRITICAL", logger="bot_module.main_app_feed"):
        assert report_main_app_feed_unreachable_once(LIVE_FEED) is False
    assert not [r for r in caplog.records if r.levelname == "CRITICAL"]
