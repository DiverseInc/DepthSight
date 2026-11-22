"""A FORK must never ship a URL that points at the upstream project's deployment.

THE DEFECT
----------
This repository is a fork of ``DepthSight-Pro/DepthSight``.  The fork lost
three pieces of the upstream's screener configuration and gained one default
that should never have existed:

1. ``MAIN_APP_WS_URL`` lost its env override and was hardcoded to
   ``wss://screener.depthsight.pro/ws/bot/`` -- the UPSTREAM's reserved
   hostname.  A fork that cannot be re-pointed at its own feed is not
   configurable at all, and the inherited default points at a deployment this
   fork never controlled (that host answers HTTP 525).
2. ``MAIN_APP_WS_ENABLED`` defaulted to ``"true"`` where the upstream's own
   default is ``"false"``.  So every deployment ran a reconnect loop against a
   host that can never answer -- the HTTP 525 storm recorded on 2026-09-23,
   which commit a60e601 throttled instead of fixing.
3. ``SYMBOL_SOURCE_MODE`` lost its env override.

The revenue bug underneath all three: a DYNAMIC-mode strategy starts, reports
"running", and silently NEVER TRADES -- because the symbol source is empty and
there is no automatic fallback to the static list.  That is the worst failure
mode a product has, because it looks healthy.

WHAT THIS FILE PINS
-------------------
* (a) an empty ``MAIN_APP_WS_URL`` with symbol source MAIN_APP is a LOUD
      refusal -- the consumer does not start, and a DYNAMIC strategy is not
      started -- never a silent zero-symbol start;
* (b) an UPSTREAM host in ``MAIN_APP_WS_URL`` is rejected at startup;
* (c) ``MAIN_APP_WS_ENABLED`` defaults to ``false`` when the env var is unset;
* (d) the env override actually wins over the default;
* (e) a legitimate non-upstream host is ACCEPTED -- no false positive;
* (f) THIS fork's OWN frontend socket (``/ws``, ``/ws/main``) is REFUSED as a
      feed -- it is the frontend's token-authenticated route, it speaks the
      frontend's messages, and nothing in this repository publishes the bot's
      ``active_pairs_update`` message, so it can never deliver a symbol.  It
      is the URL this fork used to ship in ``.env.example``;
* (g) a DYNAMIC start is REFUSED when the feed is switched OFF: there is then
      no symbol source at all, and there is no automatic fallback to the
      static list;
* (h) a rehydrate REPLAY of an already-running DYNAMIC strategy is NOT
      refused -- it must not vanish on restart -- but it is CRITICAL-logged.

Every test drives the REAL production code: the real ``bot_module.config``
module, the real ``DataConsumer.start``, the real API route, and the real
Redis command listener.  Nothing here re-implements the behaviour under test.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import fnmatch
import importlib
import json
import logging
import os
from pathlib import Path

import pytest
from httpx import AsyncClient

from api import crud, schemas
from bot_module import config as bot_config
from bot_module import controller as controller_module
from bot_module.controller import TradingController
from bot_module.data_consumer import DataConsumer
from bot_module.regime_feed import screener_feed_can_deliver_regime_data


CONFIG_ID = "0123456789abcdef0123456789abcdef"

REPO_ROOT = Path(__file__).resolve().parents[1]

# This fork's own screener WebSocket.  The route in this repository is /ws,
# with /ws/main as an alias (api/websocket_server.py).
FORK_OWN_WS_URL = "wss://depthsight.diverseinc.net/ws/main"

# A screener service the OPERATOR controls: a host that publishes the bot's
# "active_pairs_update" message.  THIS is what a working feed looks like.
#
# FORK_OWN_WS_URL is NOT one, and cannot be: it is this fork's own frontend
# socket, which is token-authenticated, speaks the frontend's messages, and
# has no publisher of "active_pairs_update" anywhere in this repository.  The
# bot now REFUSES it as a feed -- see bot_module/screener_feed.py.
OPERATOR_PUBLISHER_WS_URL = "wss://screener.my-own-domain.example/ws/bot/"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reloaded_config(monkeypatch):
    """Re-import ``bot_module.config`` with a controlled environment.

    The env override is read at IMPORT time, so the only honest way to test
    it is to re-import the module with the environment set first.
    ``importlib.reload`` mutates the module object IN PLACE, so every other
    module that did ``from bot_module import config`` sees the new values --
    which is exactly why both the environment and the module are restored on
    teardown.
    """
    import bot_module.config as cfg

    keys = ("MAIN_APP_WS_URL", "MAIN_APP_WS_ENABLED", "SYMBOL_SOURCE_MODE")
    saved = {k: os.environ.get(k) for k in keys}

    def _load(**env):
        for key in keys:
            value = env.get(key)
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        importlib.reload(cfg)
        return cfg

    yield _load

    # monkeypatch has not run its own teardown yet, so restore by hand BEFORE
    # reloading, otherwise the module would keep the test's values.
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    importlib.reload(cfg)


@pytest.fixture
def feed_env(monkeypatch):
    """Set the feed's configuration on the LIVE bot config module."""

    def _set(*, source_mode="MAIN_APP", ws_enabled=True, ws_url=""):
        monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", source_mode)
        monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", ws_enabled)
        monkeypatch.setattr(bot_config, "MAIN_APP_WS_URL", ws_url)

    return _set


@pytest.fixture
def created_strategies(monkeypatch):
    """Record calls to ``create_strategy_instance``.

    The spy returns None so the handler logs and returns instead of walking
    into real strategy construction.  Whether the spy fired is the observable
    that separates "the guard let the command through" from "the handler
    refused it".  Same pattern as tests/test_strategy_command_regime_change_guard.py.
    """
    calls = []

    def _spy(strategy_name, params=None):
        calls.append(strategy_name)
        return None

    monkeypatch.setattr(controller_module, "create_strategy_instance", _spy)
    return calls


class _FakeRedis:
    """A Redis stand-in: the fake pubsub stops the listener's `while self._running:` loop."""

    def __init__(self):
        self.published: list = []
        self.controller = None

    async def scan_iter(self, match=None, count=None):
        return
        yield  # pragma: no cover - an empty async generator

    async def get(self, key):
        return None

    async def set(self, key, value):
        return None

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
        controller = self._redis.controller
        if controller is not None:
            controller._running = False
        _channel, message = self._redis.published[-1]
        return {"type": "message", "data": message}


def _controller(redis_client=None, get_db_session=None):
    """A minimal TradingController.

    Uses ``__new__`` + set-state-by-hand, the pattern already established in
    tests/test_strategy_command_regime_change_guard.py.  Only the attributes
    the Redis command listener and ``_handle_start_strategy_command`` touch
    are set.
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
    *, symbol_selection_mode: str | None = "DYNAMIC", rehydrate: bool = False
):
    """A ``START_STRATEGY`` command in the exact envelope the API publishes.

    ``rehydrate`` marks a REPLAY of a strategy that was already approved and
    was running before the bot restarted; the listener passes it straight
    through to the handler (controller.py:1568).
    """
    payload = {
        "user_id": 1,
        "id": CONFIG_ID,
        "config_id": CONFIG_ID,
        "mode": "paper",
        "config_data": {"strategy_name": "VisualBuilderStrategy"},
    }
    if symbol_selection_mode is not None:
        payload["symbol_selection_mode"] = symbol_selection_mode
    command = {"command": "START_STRATEGY", "payload": payload}
    if rehydrate:
        command["rehydrate"] = True
    return command


async def _deliver(controller, command):
    """Run the real Redis command listener until it has handled ``command``."""
    redis_client = _FakeRedis()
    redis_client.controller = controller
    redis_client.published.append(("depthsight:commands", json.dumps(command)))
    controller.redis_client = redis_client
    controller._running = True
    await controller._redis_command_listener()


async def _make_config(
    db_session, *, user_id: int, name: str, mode: str, config_data: dict
):
    """Create a real strategy config so the endpoint runs its real code path."""
    created = await crud.create_strategy_config(
        db_session,
        user_id=user_id,
        config_create=schemas.StrategyConfigCreate(
            name=name,
            config_data=config_data,
            symbol_selection_mode=mode,
        ),
    )
    await db_session.commit()
    await db_session.refresh(created)
    return created


async def _launch(client: AsyncClient, config_id: str, **extra):
    body = {"config_id": config_id}
    body.update(extra)
    return await client.post("/api/v1/strategies", json=body)


async def _start_consumer(feed_env, *, ws_url: str, ws_enabled: bool = True):
    """Start the REAL DataConsumer and return it.

    The WebSocket loop itself is replaced by a no-op: what is under test is
    whether the consumer REFUSES to start, not whether it can connect.
    """
    feed_env(source_mode="MAIN_APP", ws_enabled=ws_enabled, ws_url=ws_url)
    consumer = DataConsumer(
        loop=asyncio.get_running_loop(),
        controller=None,
        market_data_mode="direct",
    )

    async def _noop_ws_loop():
        return None

    consumer._main_app_ws_loop = _noop_ws_loop
    try:
        await consumer.start()
    finally:
        task = getattr(consumer, "_main_app_ws_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return consumer


# ---------------------------------------------------------------------------
# (c) the defaults, and (d) the env overrides
# ---------------------------------------------------------------------------


async def test_main_app_ws_enabled_defaults_to_false_when_the_env_var_is_unset(
    reloaded_config,
):
    """(c) The fork's default must be the upstream's SAFE default: OFF.

    The fork had this inverted to "true", so every deployment started a
    reconnect loop against a feed that could never work.
    """
    cfg = reloaded_config(MAIN_APP_WS_ENABLED=None)

    assert cfg.MAIN_APP_WS_ENABLED is False, (
        "MAIN_APP_WS_ENABLED must default to False. The upstream project "
        "defaults it to off (ENABLE_MAIN_APP_WS defaults to \"false\"); the fork "
        "inverted it to \"true\", so every deployment ran a reconnect loop "
        f"against a feed that could never work. Got {cfg.MAIN_APP_WS_ENABLED!r}."
    )


async def test_main_app_ws_url_has_no_default_so_the_fork_never_ships_the_upstream_url(
    reloaded_config,
):
    """The fork must ship NO screener URL at all, not the upstream's.

    An inherited upstream URL is a footgun: this fork never controlled that
    DNS, the host is not this fork's deployment, and it answers HTTP 525.
    """
    cfg = reloaded_config(MAIN_APP_WS_URL=None)

    assert cfg.MAIN_APP_WS_URL == "", (
        "MAIN_APP_WS_URL must default to the EMPTY string. The upstream's "
        "default (wss://screener.depthsight.pro/ws/bot/) points at the "
        "upstream's reserved hostname, which is not this fork's deployment. "
        f"Got {cfg.MAIN_APP_WS_URL!r}."
    )
    assert "depthsight.pro" not in cfg.MAIN_APP_WS_URL, (
        "no default in this fork may name the upstream's domain. "
        f"Got {cfg.MAIN_APP_WS_URL!r}."
    )


async def test_main_app_ws_url_env_override_wins_over_the_default(reloaded_config):
    """(d) The fork must be configurable to use a working feed.

    The env override was LOST in the fork, so the only way to change the URL
    was to edit source.
    """
    cfg = reloaded_config(MAIN_APP_WS_URL=FORK_OWN_WS_URL)

    assert cfg.MAIN_APP_WS_URL == FORK_OWN_WS_URL, (
        "MAIN_APP_WS_URL must come from the environment. The fork hardcoded "
        f"it, so it could not be configured at all. Got {cfg.MAIN_APP_WS_URL!r}."
    )


async def test_symbol_source_mode_env_override_wins_over_the_default(reloaded_config):
    """(d) The symbol source mode is a deployment setting."""
    cfg = reloaded_config(SYMBOL_SOURCE_MODE="STATIC_LIST")

    assert cfg.SYMBOL_SOURCE_MODE == "STATIC_LIST", (
        "SYMBOL_SOURCE_MODE must come from the environment. The fork "
        f"hardcoded it. Got {cfg.SYMBOL_SOURCE_MODE!r}."
    )


async def test_main_app_ws_enabled_env_override_wins_over_the_default(reloaded_config):
    """(d) The enabled flag must still be settable.

    NOTE: the fork honoured THIS override already -- its regression was the
    DEFAULT, not the override -- so this test is not a regression witness.
    It is pinned so the default fix cannot be "fixed" by dropping the
    override.
    """
    cfg = reloaded_config(MAIN_APP_WS_ENABLED="true")

    assert cfg.MAIN_APP_WS_ENABLED is True, (
        f"MAIN_APP_WS_ENABLED must be settable. Got {cfg.MAIN_APP_WS_ENABLED!r}."
    )


async def test_symbol_source_mode_defaults_to_main_app(reloaded_config):
    """The upstream's default source mode is preserved."""
    cfg = reloaded_config(SYMBOL_SOURCE_MODE=None)

    assert cfg.SYMBOL_SOURCE_MODE == "MAIN_APP", (
        f"SYMBOL_SOURCE_MODE must default to 'MAIN_APP'. Got {cfg.SYMBOL_SOURCE_MODE!r}."
    )


def test_the_shipped_env_example_advertises_no_feed_that_cannot_deliver():
    """(i) The SHIPPED default must be correct.

    This fork used to ship ``MAIN_APP_WS_URL=wss://depthsight.diverseinc.net/ws/main``
    in ``.env.example``.  That URL is THIS fork's own FRONTEND socket: it is
    token-authenticated (api/websocket_server.py:228-230), it forwards Redis
    messages as ``{"topic", "payload"}`` (:187-191) while the bot only
    understands ``{"type": "active_pairs_update", "data": [...]}``
    (data_consumer.py:3476-3478), and nothing in this repository publishes
    that message at all.  An operator who copied the shipped default got a
    feed that connects and delivers NO SYMBOL -- so a DYNAMIC strategy
    reported "running" and never traded.

    The shipped default must therefore be FAIL-CLOSED: it must not advertise
    a URL the bot cannot speak to, and a DYNAMIC strategy on the shipped
    configuration must be REFUSED rather than started into a silent no-op.

    This reads the real ``.env.example`` -- the file an operator actually
    copies -- and not the source default, because the source default and the
    shipped example can drift apart.
    """
    env_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    values = {}
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    url = values.get("MAIN_APP_WS_URL", "")
    enabled = values.get("MAIN_APP_WS_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    rules = _shared_rules()

    # This fork ships NO screener URL: it runs no screener service
    # (docker-compose.yml has none), so there is no correct URL to ship.
    # The upstream's reserved hostname and this fork's own frontend socket
    # are the two that used to be shipped here; neither can deliver a symbol.
    assert url == "", (
        ".env.example must not advertise a MAIN_APP_WS_URL: this fork runs no "
        "screener service, and the URL it used to ship "
        "(wss://depthsight.diverseinc.net/ws/main) is the FRONTEND's "
        f"token-authenticated socket, which cannot deliver a symbol to the "
        f"bot. Got {url!r}."
    )
    assert not rules.is_fork_own_frontend_ws_url(url), (
        ".env.example must never advertise this fork's own frontend socket as "
        f"the screener feed. Got {url!r}."
    )
    assert not rules.is_upstream_depthsight_url(url), (
        ".env.example must never advertise the UPSTREAM's reserved hostname as "
        f"the screener feed. Got {url!r}."
    )

    # The feed is OFF by default, and the shipped default is therefore
    # REFUSED: a DYNAMIC strategy cannot start against it.
    assert enabled is False, (
        ".env.example must leave MAIN_APP_WS_ENABLED=false: the shipped "
        "default has no screener behind it, so switching the feed on would "
        "start a reconnect storm against nothing."
    )
    problem = rules.screener_feed_misconfigured("MAIN_APP", url, enabled)
    assert problem is not None, (
        "the SHIPPED default must be fail-closed: with no screener URL and the "
        "feed off, a DYNAMIC strategy must be REFUSED, not started into a "
        "strategy with zero symbols that reports 'running' and never trades."
    )
    assert "MAIN_APP_WS_ENABLED" in problem, (
        f"the refusal must name the switch the operator has to flip. Got: {problem!r}"
    )


# ---------------------------------------------------------------------------
# the shared rules in bot_module/screener_feed.py
# ---------------------------------------------------------------------------


def _shared_rules():
    """The new module, imported LAZILY.

    Imported inside the function so this whole file still COLLECTS and RUNS
    against the unfixed tree: the other tests must be able to fail with a
    real assertion against the pre-fix code, not a collection error.
    """
    from bot_module import screener_feed

    return screener_feed


def test_the_shared_rule_names_the_missing_env_var():
    """The empty URL must be reported as the MISSING env var, by name."""
    problem = _shared_rules().describe_ws_url_problem("")

    assert problem is not None, "an empty MAIN_APP_WS_URL must be reported"
    assert "MAIN_APP_WS_URL" in problem, (
        f"the message must name the missing env var. Got: {problem!r}"
    )
    assert "not set" in problem, (
        f"the message must say the URL is not set. Got: {problem!r}"
    )


def test_the_shared_rule_names_the_upstream_host_and_points_at_this_fork():
    """The upstream host must be named, and this fork's own domain offered."""
    rules = _shared_rules()
    problem = rules.describe_ws_url_problem("wss://screener.depthsight.pro/ws/bot/")

    assert problem is not None, (
        "the upstream's host must be reported as a problem"
    )
    assert "depthsight.pro" in problem, (
        f"the message must name the offending upstream host. Got: {problem!r}"
    )
    assert rules.FORK_OWN_DOMAIN in problem, (
        f"the message must point at THIS fork's own domain. Got: {problem!r}"
    )


def test_the_shared_rule_refuses_a_url_that_is_not_a_websocket():
    """An http:// URL is not a WebSocket endpoint."""
    problem = _shared_rules().describe_ws_url_problem(
        "https://depthsight.diverseinc.net/ws/main"
    )

    assert problem is not None, (
        "a URL that is not ws:// or wss:// must be reported as a problem"
    )
    assert "ws://" in problem, (
        f"the message must say what a WebSocket URL looks like. Got: {problem!r}"
    )


def test_the_shared_rule_accepts_a_legitimate_host():
    """A legitimate host must not be reported as a problem.

    This is the URL rule: it asks "is this a usable WebSocket URL?".  It is
    NOT the question "can this feed deliver a symbol to the bot?" -- that
    one is ``screener_feed_misconfigured``, and it refuses this fork's own
    frontend socket (see the next test).
    """
    rules = _shared_rules()

    assert rules.describe_ws_url_problem(FORK_OWN_WS_URL) is None, (
        "this fork's own domain is a well-formed WebSocket URL and must not "
        "be mistaken for the upstream's"
    )
    assert rules.describe_ws_url_problem(
        "wss://screener.mycompany-depthsight.pro/ws/main"
    ) is None, (
        "a host the owner controls must be accepted: the upstream check is "
        "exact-host based, not a suffix or substring match"
    )


def test_the_shared_rule_refuses_a_websocket_url_with_no_host():
    """A ws:// URL that names NO HOST is not a WebSocket endpoint.

    The NOT-A-WS check is a SCHEME PREFIX test, so 'ws://' and
    'ws:///ws/main' used to pass it: they look like WebSocket URLs, and they
    name no host at all. There is nothing to connect to, so the feed
    reconnects forever against an empty address while the deployment keeps
    looking healthy -- the exact failure this guard exists to prevent.
    """
    rules = _shared_rules()

    for url in ("ws://", "ws:///ws/main", "wss://"):
        problem = rules.describe_ws_url_problem(url)

        assert problem is not None, (
            f"a WebSocket URL with NO HOST must be reported as a problem. "
            f"Got: {url!r}"
        )
        assert "MAIN_APP_WS_URL" in problem, (
            f"the message must name the env var. Got: {problem!r}"
        )
        assert "host" in problem.lower(), (
            f"the message must NAME the bad state (no host). Got: {problem!r}"
        )


def test_the_shared_rule_refuses_this_forks_own_frontend_socket():
    """THIS fork's own frontend socket cannot deliver a symbol to the bot.

    api/websocket_server.py serves /ws (:221) and /ws/main (:390) as an
    alias.  That route is the FRONTEND's socket: it closes every connection
    that carries no "?token=" (:228-230) and the bot's screener client
    sends no token (data_consumer.py:3414-3437); it forwards Redis messages
    as {"topic", "payload"} (:187-191) while the bot only understands
    {"type", "active_pairs_update", "data"} (data_consumer.py:3476-3478);
    and nothing in this repository publishes that message at all.

    So the URL this fork used to SHIP in .env.example
    (wss://depthsight.diverseinc.net/ws/main) is a DEAD feed: a DYNAMIC
    strategy pointed at it reports "running" and never trades.
    """
    rules = _shared_rules()

    for url in (
        "wss://depthsight.diverseinc.net/ws/main",
        "wss://depthsight.diverseinc.net/ws",
        "wss://depthsight.diverseinc.net/ws/main/",
        "wss://depthsight.diverseinc.net:8443/ws/main",
    ):
        problem = rules.screener_feed_misconfigured("MAIN_APP", url, True)

        assert problem is not None, (
            "this fork's own frontend socket must be refused as a feed: it "
            f"can never deliver a symbol. Got: {url!r}"
        )
        assert "MAIN_APP_WS_URL" in problem, (
            f"the message must name the env var. Got: {problem!r}"
        )
        assert "frontend" in problem.lower(), (
            "the message must NAME the bad state (it is the frontend's "
            f"socket). Got: {problem!r}"
        )


def test_the_shared_rule_accepts_an_operator_owned_publisher():
    """An operator's own screener host must NOT be refused -- no false positive."""
    rules = _shared_rules()

    assert rules.screener_feed_misconfigured(
        "MAIN_APP", OPERATOR_PUBLISHER_WS_URL, True
    ) is None, (
        "a host the operator controls must be accepted: refusing it would "
        "be a false positive"
    )
    assert rules.screener_feed_misconfigured(
        "MAIN_APP", "wss://screener.my-own-domain.example/ws/main", True
    ) is None, (
        "the fork's own frontend socket is refused by HOST AND PATH, so an "
        "operator's own host on the same path is still accepted"
    )
    assert rules.screener_feed_misconfigured(
        "MAIN_APP", "wss://depthsight.diverseinc.net/ws/bot/", True
    ) is None, (
        "only the fork's own two frontend routes are refused: another path on "
        "the same host may be the operator's own screener, reverse-proxied"
    )


def test_the_shared_rule_refuses_a_feed_that_is_switched_off():
    """A switched-off feed has NO symbol source at all.

    The bot's DYNAMIC symbol pool is _last_known_symbols_from_consumer
    (controller.py:7216) and that set is filled ONLY by the screener feed's
    "active_pairs_update" message.  With the feed off there is no fallback to
    the static list, so a DYNAMIC strategy started here has ZERO symbols: it
    reports "running" and never trades.
    """
    rules = _shared_rules()

    problem = rules.screener_feed_misconfigured("MAIN_APP", "", False)

    assert problem is not None, (
        "a DYNAMIC start with the feed switched off must be refused: there "
        "is no symbol source and no fallback to the static list"
    )
    assert "MAIN_APP_WS_ENABLED" in problem, (
        f"the message must name the switch that is off. Got: {problem!r}"
    )


def test_the_shared_rule_refuses_an_unusable_url_even_when_the_feed_is_off():
    """A URL that is set but unusable is wrong in BOTH pairings.

    The URL rules used to be asked only when the feed was switched ON, so
    "the feed is off" was a way to smuggle an unusable URL past the guard.
    It is the cause the moment the feed is switched on, and it is the wrong
    configuration right now.
    """
    rules = _shared_rules()

    for url in ("https://screener.my-own-domain.example/ws/bot/", "ws://"):
        problem = rules.screener_feed_misconfigured("MAIN_APP", url, False)

        assert problem is not None, (
            "an unusable URL must be refused whatever the enabled flag says. "
            f"Got: {url!r}"
        )
        assert "ws://" in problem, (
            f"the message must say what a WebSocket URL looks like. "
            f"Got: {problem!r}"
        )


def test_the_regime_predicate_refuses_this_forks_own_frontend_socket(feed_env):
    """The regime predicate must handle the same special cases as the guard.

    It answers "can THIS deployment deliver the regime data?" and the regime
    data arrives with the bot's "active_pairs_update" message -- so a feed
    pointed at the frontend's socket cannot deliver it either, and a regime
    stop armed against it would be inert from the first second.
    """
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url=FORK_OWN_WS_URL)

    assert screener_feed_can_deliver_regime_data() is False, (
        "this fork's own frontend socket cannot deliver the regime data, so "
        "the predicate must say so -- otherwise a protection that can never "
        "fire is armed silently"
    )


def test_the_regime_predicate_still_accepts_an_operator_owned_publisher(feed_env):
    """No false positive: an operator's own screener still counts."""
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url=OPERATOR_PUBLISHER_WS_URL)

    assert screener_feed_can_deliver_regime_data() is True, (
        "an operator's own screener can deliver the regime data: refusing it "
        "would disarm a protection that WOULD have worked"
    )


def test_the_regime_predicate_refuses_the_upstream(feed_env):
    """The upstream's reserved hostname cannot deliver the regime data.

    The shared symbol guard refuses it, so the regime predicate must refuse
    it too: AGENTS.md requires duplicated predicates to handle the same
    special cases, and a regime stop armed against the upstream is inert
    from the first second.
    """
    feed_env(
        source_mode="MAIN_APP",
        ws_enabled=True,
        ws_url="wss://screener.depthsight.pro/ws/bot/",
    )

    assert screener_feed_can_deliver_regime_data() is False, (
        "the upstream's deployment is not this fork and answers HTTP 525, so "
        "it cannot deliver the regime data"
    )


async def test_a_hostless_websocket_url_makes_the_feed_undeliverable(feed_env):
    """A ws:// URL with NO HOST cannot deliver the regime data.

    The predicate answers "can THIS deployment deliver the regime data?".
    A URL that names no host cannot: there is no host to connect to, and the
    consumer refuses to start against it. Answering True would arm a
    regime-change stop against a feed that can never deliver.
    """
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url="ws://")

    assert screener_feed_can_deliver_regime_data() is False, (
        "a WebSocket URL with no host cannot deliver the regime data, so the "
        "predicate must say so -- otherwise a protection that can never fire "
        "is armed silently"
    )


# ---------------------------------------------------------------------------
# (a) the regime-change stop must not be armed on a feed that cannot deliver
# ---------------------------------------------------------------------------


async def test_an_empty_url_makes_the_feed_undeliverable_so_arming_the_stop_is_refused(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    monkeypatch,
):
    """THE PRODUCTION CASE: the shipped defaults cannot deliver the regime data.

    This test deliberately uses the CODE DEFAULTS rather than a patched
    config, because the code defaults are what the deployment runs. Before
    the fix the URL defaulted to the upstream's host and MAIN_APP_WS_ENABLED
    defaulted to true, so all three conditions of
    screener_feed_can_deliver_regime_data() PASSED and the stop was armed --
    inert from the first second, because its only data source answers 525.
    """
    # The env vars are removed so a developer's own .env cannot change what
    # "the shipped defaults" means. bot_module.config was already imported,
    # so this pins the import-time defaults.
    for var in ("MAIN_APP_WS_URL", "MAIN_APP_WS_ENABLED", "SYMBOL_SOURCE_MODE"):
        monkeypatch.delenv(var, raising=False)

    assert screener_feed_can_deliver_regime_data() is False, (
        "with the shipped defaults the screener feed cannot deliver the "
        "regime data, and the predicate must say so -- otherwise a "
        "protection that can never fire is armed silently"
    )

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-with-an-empty-url",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "arming breakeven_on_regime_change must be refused with 503 when the "
        "deployment has no screener feed to deliver the regime data. "
        f"Got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "breakeven_on_regime_change" in body["error"], (
        f"the 503 must name the option it refused. Got: {body!r}"
    )


# ---------------------------------------------------------------------------
# (C) the SAFE pairing must keep the bot alive
# ---------------------------------------------------------------------------


async def test_the_consumer_still_runs_when_the_feed_is_disabled(feed_env):
    """(C) WS_ENABLED=false must NOT take the market data down with it.

    The UNSAFE pairing (the feed enabled but the URL unusable) refuses to
    start the consumer. The SAFE pairing -- the feed switched off -- must
    leave the DataConsumer running, so the bot keeps its candles and its
    pinned-symbol strategies keep trading.
    """
    consumer = await _start_consumer(feed_env, ws_url="", ws_enabled=False)

    assert consumer._running is True, (
        "with MAIN_APP_WS_ENABLED=false the DataConsumer must still start: "
        "the screener loop is skipped, but the market data must keep flowing. "
        "Killing the whole consumer here would take the bot's candles down "
        "with a configuration typo."
    )
    assert consumer._main_app_ws_task is None, (
        "the screener WebSocket loop must NOT be started when the feed is "
        "disabled by configuration"
    )


async def test_the_bot_still_starts_a_fixed_strategy_with_a_broken_feed(
    created_strategies, feed_env
):
    """A FIXED (pinned-symbol) strategy needs no feed, so it must not be refused."""
    feed_env(source_mode="MAIN_APP", ws_enabled=False, ws_url="")

    controller = _controller()

    await _deliver(controller, _start_command(symbol_selection_mode="FIXED"))

    assert created_strategies == ["VisualBuilderStrategy"], (
        "a FIXED strategy with pinned symbols must still be started on a "
        f"deployment whose screener feed is off. Got {created_strategies!r}."
    )


# ---------------------------------------------------------------------------
# (a) + (b) + (e) at the consumer: the real DataConsumer.start()
# ---------------------------------------------------------------------------


async def test_the_consumer_refuses_to_start_when_the_url_is_missing(feed_env, caplog):
    """(a) An empty MAIN_APP_WS_URL must be a LOUD refusal.

    Not a silent zero-symbol start: the consumer must not start at all, and
    the log must name the env var the operator has to set.
    """
    with caplog.at_level(logging.CRITICAL, logger="bot_module.data_consumer"):
        consumer = await _start_consumer(feed_env, ws_url="")

    assert consumer._running is False, (
        "The DataConsumer must REFUSE to start when MAIN_APP_WS_URL is empty "
        "and the symbol source is MAIN_APP. Starting anyway is the silent "
        "zero-symbol start this guard exists to prevent."
    )
    assert "MAIN_APP_WS_URL" in caplog.text, (
        "the refusal must NAME the missing env var. Log: " + caplog.text
    )


async def test_the_consumer_refuses_to_start_when_the_url_points_at_the_upstream(
    feed_env, caplog
):
    """(b) An upstream host must be rejected at startup."""
    with caplog.at_level(logging.CRITICAL, logger="bot_module.data_consumer"):
        consumer = await _start_consumer(
            feed_env, ws_url="wss://screener.depthsight.pro/ws/bot/"
        )

    assert consumer._running is False, (
        "The DataConsumer must REFUSE to start when MAIN_APP_WS_URL points at "
        "the UPSTREAM DepthSight-Pro deployment. That host is not this fork "
        "and answers HTTP 525, so the only possible outcome is a reconnect "
        "storm."
    )
    assert "depthsight.pro" in caplog.text, (
        "the refusal must name the offending upstream host. Log: " + caplog.text
    )
    assert "depthsight.diverseinc.net" in caplog.text, (
        "the refusal must point at THIS fork's own domain. Log: " + caplog.text
    )


async def test_the_consumer_starts_with_a_legitimate_url(feed_env):
    """(e) A legitimate, non-upstream host must be ACCEPTED.

    The guard must not refuse a deployment the owner controls.
    """
    consumer = await _start_consumer(feed_env, ws_url=FORK_OWN_WS_URL)

    assert consumer._running is True, (
        "A legitimate MAIN_APP_WS_URL must be accepted: the consumer has to "
        "start. This fork's own domain, or any other host the owner "
        "controls, is valid."
    )
    assert consumer._main_app_ws_task is not None, (
        "the screener WebSocket loop must actually be started for a "
        "legitimate URL."
    )


async def test_the_consumer_starts_with_a_legitimate_url_on_another_host(feed_env):
    """(e) The upstream check is EXACT-HOST based, so other hosts are fine."""
    consumer = await _start_consumer(
        feed_env, ws_url="wss://screener.my-own-domain.example/ws/main"
    )

    assert consumer._running is True, (
        "the upstream check must be exact-host based: a deployment on the "
        "owner's own host must never be refused."
    )


async def test_the_consumer_starts_with_a_host_that_ends_with_the_upstream_domain(
    feed_env,
):
    """(e) A host that merely ENDS with the upstream's domain is the owner's own.

    A suffix test ("endswith('depthsight.pro')") would refuse this
    deployment, which is a false positive: the owner may control any domain
    they like.
    """
    consumer = await _start_consumer(
        feed_env, ws_url="wss://screener.mycompany-depthsight.pro/ws/main"
    )

    assert consumer._running is True, (
        "the upstream check must compare the WHOLE host, not a suffix of it: "
        "a host the owner controls must never be refused."
    )


async def test_the_consumer_starts_with_a_host_that_contains_the_upstream_domain(
    feed_env,
):
    """(e) A host that merely CONTAINS the upstream's domain is the owner's own.

    A substring test ("'depthsight.pro' in host") would refuse this
    deployment, and would also refuse a legitimate host such as
    "screener.depthsight.pro.example.invalid".
    """
    consumer = await _start_consumer(
        feed_env, ws_url="wss://depthsight.pro.example.invalid/ws/main"
    )

    assert consumer._running is True, (
        "the upstream check must compare the WHOLE host, not a substring of "
        "it: a host the owner controls must never be refused."
    )


async def test_the_consumer_refuses_to_start_when_the_url_is_not_a_websocket_url(
    feed_env, caplog
):
    """An http:// URL is not a WebSocket endpoint, so the feed cannot run."""
    with caplog.at_level(logging.CRITICAL, logger="bot_module.data_consumer"):
        consumer = await _start_consumer(
            feed_env, ws_url="https://depthsight.diverseinc.net/ws/main"
        )

    assert consumer._running is False, (
        "the consumer must refuse to start when MAIN_APP_WS_URL is not a "
        "ws:// or wss:// URL: there is no WebSocket to connect to."
    )
    assert "MAIN_APP_WS_URL" in caplog.text, (
        "the refusal must name the env var. Log: " + caplog.text
    )


# ---------------------------------------------------------------------------
# (a) + (b) + (e) at the API route: POST /api/v1/strategies
# ---------------------------------------------------------------------------


async def test_the_api_refuses_a_dynamic_strategy_when_the_url_is_missing(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(a) The HTTP door must refuse a DYNAMIC strategy with no symbol source.

    Before the fix the strategy started, reported "running", and never traded.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url="")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-without-a-feed",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "A DYNAMIC strategy must be refused with 503 when this deployment has "
        "no symbol source: it would start with zero symbols, report 'running' "
        f"and never trade. Got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "MAIN_APP_WS_URL" in body["error"], (
        f"the 503 must name the missing env var. Got: {body!r}"
    )


async def test_the_api_refuses_a_dynamic_strategy_when_the_url_points_at_the_upstream(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(b) The HTTP door must refuse an upstream-pointing feed."""
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(
        source_mode="MAIN_APP",
        ws_enabled=True,
        ws_url="wss://screener.depthsight.pro/ws/bot/",
    )

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-pointing-at-the-upstream",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "A DYNAMIC strategy must be refused with 503 when MAIN_APP_WS_URL "
        "points at the UPSTREAM DepthSight-Pro deployment. "
        f"Got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "depthsight.pro" in body["error"], (
        f"the 503 must name the offending upstream host. Got: {body!r}"
    )


async def test_the_api_still_launches_a_dynamic_strategy_with_a_working_feed(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(e) A working feed must NOT be refused -- no false positive.

    A working feed is a host the OPERATOR controls that publishes the bot's
    "active_pairs_update" message.  It is NOT this fork's own frontend
    socket: that route is token-authenticated, speaks the frontend's
    messages, and has no publisher of "active_pairs_update" in this
    repository -- so it can never deliver a symbol (measured; see
    test_the_shared_rule_refuses_this_forks_own_frontend_socket).
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(
        source_mode="MAIN_APP", ws_enabled=True, ws_url=OPERATOR_PUBLISHER_WS_URL
    )

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-with-a-working-feed",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        "A DYNAMIC strategy with a working feed must still be accepted: "
        f"refusing it would be a false positive. Got {response.status_code}: "
        f"{response.text}"
    )


async def test_the_api_refuses_a_dynamic_strategy_when_the_feed_is_disabled(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(g) A switched-off feed has NO symbol source -- refuse it.

    MAIN_APP_WS_ENABLED=false means nothing can deliver a symbol: the bot's
    DYNAMIC symbol pool is filled ONLY by the screener feed's
    "active_pairs_update" message (controller.py:7216), and there is no
    automatic fallback to the static list.  A DYNAMIC strategy accepted here
    would report "running" with ZERO symbols and never trade -- the failure
    mode that looks healthy.

    SYMBOL_SELECTION_ALLOW_DYNAMIC is set to true here, because an operator
    who enables DYNAMIC is exactly the operator who hits this: they flipped
    the switch and pointed the feed at nothing.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(source_mode="MAIN_APP", ws_enabled=False, ws_url="")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-with-the-feed-disabled",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "a DYNAMIC strategy must be refused while the screener feed is "
        "switched off: there is no symbol source and no fallback to the "
        f"static list. Got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "MAIN_APP_WS_ENABLED" in body["error"], (
        f"the 503 must name the switch that is off. Got: {body!r}"
    )


async def test_the_api_refuses_a_dynamic_strategy_pointed_at_this_forks_own_socket(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(f) This fork's own frontend socket cannot deliver a symbol.

    wss://depthsight.diverseinc.net/ws/main is the URL this fork used to
    SHIP in .env.example.  It is the frontend's token-authenticated socket
    and nothing in this repository publishes the bot's "active_pairs_update"
    message, so a DYNAMIC strategy pointed at it would report "running" and
    never trade.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url=FORK_OWN_WS_URL)

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-pointed-at-the-forks-own-socket",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "a DYNAMIC strategy must be refused while MAIN_APP_WS_URL points at "
        "this fork's own frontend socket: it can never deliver a symbol. "
        f"Got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "frontend" in body["error"].lower(), (
        f"the 503 must name the bad state. Got: {body!r}"
    )


async def test_the_api_refuses_a_dynamic_strategy_with_the_feed_on_and_no_url(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(a) The UNSAFE PAIRING: the feed is on but there is no URL.

    The consumer refuses to start in that state, so nothing would ever
    deliver a symbol -- and a DYNAMIC strategy accepted here would report
    "running" with ZERO symbols and never trade.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url="")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-with-the-feed-on-and-no-url",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "a DYNAMIC strategy must be refused while MAIN_APP_WS_ENABLED is on "
        "and MAIN_APP_WS_URL is empty: the consumer refuses to start, so "
        f"nothing would ever deliver a symbol. Got {response.status_code}: "
        f"{response.text}"
    )
    body = response.json()
    assert "MAIN_APP_WS_URL" in body["error"], (
        f"the 503 must name the env var. Got: {body!r}"
    )


async def test_the_api_refuses_a_dynamic_strategy_that_points_at_the_upstream_even_with_the_feed_off(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """(b) The upstream pointer is wrong in ANY pairing.

    It does not matter that the feed is switched off right now: the moment
    it is switched on, the bot would reconnect against the upstream's
    reserved hostname for ever.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)
    feed_env(
        source_mode="MAIN_APP",
        ws_enabled=False,
        ws_url="wss://screener.depthsight.pro/ws/bot/",
    )

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-pointing-at-the-upstream-feed-off",
        mode="DYNAMIC",
        config_data={"strategy_name": "VisualBuilderStrategy", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "a DYNAMIC strategy must be refused while MAIN_APP_WS_URL names the "
        "UPSTREAM DepthSight-Pro deployment, whatever the enabled flag says. "
        f"Got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "depthsight.pro" in body["error"], (
        f"the 503 must name the offending upstream host. Got: {body!r}"
    )


async def test_the_api_still_launches_a_static_strategy_with_a_broken_feed(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_env,
    monkeypatch,
):
    """A strategy with explicit symbols needs no feed, so it must not be refused."""
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url="")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="static-with-a-broken-feed",
        mode="STATIC",
        config_data={
            "strategy_name": "VisualBuilderStrategy",
            "params": {},
            "symbols": ["BTCUSDT"],
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        "A strategy with explicit symbols must still be accepted on a "
        f"deployment whose screener feed is broken. Got "
        f"{response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# (a) + (b) + (e) at the bot's command channel: the real Redis listener
# ---------------------------------------------------------------------------


async def test_the_bot_refuses_to_start_a_dynamic_strategy_when_the_url_is_missing(
    created_strategies, feed_env, caplog
):
    """(a) The Redis door must refuse too -- it is a second way in."""
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url="")

    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(controller, _start_command(symbol_selection_mode="DYNAMIC"))

    assert created_strategies == [], (
        "the Redis command path must refuse to start a DYNAMIC strategy when "
        "the deployment has no symbol source -- the strategy would start, "
        "report 'running' and never trade"
    )
    assert "MAIN_APP_WS_URL" in caplog.text, (
        "the refusal must NAME the missing env var. Log: " + caplog.text
    )


async def test_the_bot_refuses_to_start_a_dynamic_strategy_when_the_url_points_at_the_upstream(
    created_strategies, feed_env, caplog
):
    """(b) The Redis door must refuse an upstream-pointing feed."""
    feed_env(
        source_mode="MAIN_APP",
        ws_enabled=True,
        ws_url="wss://screener.depthsight.pro/ws/bot/",
    )

    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(controller, _start_command(symbol_selection_mode="DYNAMIC"))

    assert created_strategies == [], (
        "the Redis command path must refuse to start a DYNAMIC strategy when "
        "MAIN_APP_WS_URL points at the UPSTREAM DepthSight-Pro deployment"
    )
    assert "depthsight.pro" in caplog.text, (
        "the refusal must name the offending upstream host. Log: " + caplog.text
    )


async def test_the_bot_refuses_a_dynamic_strategy_when_the_feed_is_disabled(
    created_strategies, feed_env, caplog
):
    """(g) A switched-off feed has NO symbol source -- refuse it.

    The bot's DYNAMIC symbol pool is filled ONLY by the screener feed's
    "active_pairs_update" message (controller.py:7216), and there is no
    automatic fallback to the static list.  A DYNAMIC strategy accepted here
    would report "running" with ZERO symbols and never trade.

    This is the Redis door, so it is a second way in besides the API's 503:
    both ask the same predicate, and both must fail in the same direction.
    """
    feed_env(source_mode="MAIN_APP", ws_enabled=False, ws_url="")

    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(controller, _start_command(symbol_selection_mode="DYNAMIC"))

    assert created_strategies == [], (
        "a DYNAMIC strategy must not be started while the screener feed is "
        "switched off: there is no symbol source and no fallback to the "
        "static list. Got " + repr(created_strategies)
    )
    assert "MAIN_APP_WS_ENABLED" in caplog.text, (
        "the refusal must name the switch that is off -- a refusal that only "
        "exists in a return statement is not an operation. Log: " + caplog.text
    )


async def test_the_bot_refuses_a_dynamic_strategy_pointed_at_this_forks_own_socket(
    created_strategies, feed_env, caplog
):
    """(f) This fork's own frontend socket cannot deliver a symbol.

    wss://depthsight.diverseinc.net/ws/main is the URL this fork used to
    SHIP in .env.example.  It is the frontend's token-authenticated socket
    and nothing in this repository publishes the bot's "active_pairs_update"
    message, so a DYNAMIC strategy pointed at it would report "running" and
    never trade.
    """
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url=FORK_OWN_WS_URL)

    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(controller, _start_command(symbol_selection_mode="DYNAMIC"))

    assert created_strategies == [], (
        "a DYNAMIC strategy must not be started while MAIN_APP_WS_URL points "
        "at this fork's own frontend socket: it can never deliver a symbol. "
        "Got " + repr(created_strategies)
    )
    assert "frontend" in caplog.text.lower(), (
        "the refusal must name the bad state. Log: " + caplog.text
    )


async def test_the_bot_still_replays_a_dynamic_strategy_when_the_feed_cannot_deliver(
    created_strategies, feed_env, caplog
):
    """(h) A rehydrate REPLAY is exempt from the refusal -- but not silent.

    The replayed strategy was already approved by the API and was running
    before this restart, so refusing it would make the user's running
    strategy VANISH after a restart -- a worse regression than a strategy
    that cannot pick new symbols.  It is exempt, and it is CRITICAL-logged,
    exactly like the replayed regime arming.
    """
    feed_env(source_mode="MAIN_APP", ws_enabled=False, ws_url="")

    controller = _controller()

    with caplog.at_level(logging.CRITICAL, logger="bot_module.controller"):
        await _deliver(
            controller,
            _start_command(symbol_selection_mode="DYNAMIC", rehydrate=True),
        )

    assert created_strategies == ["VisualBuilderStrategy"], (
        "a replayed strategy must not vanish on restart. Got "
        + repr(created_strategies)
    )
    assert "REPLAYED" in caplog.text, (
        "the replay must be LOUD: it is exempt from the refusal, so nothing "
        "else would say that its feed cannot deliver. Log: " + caplog.text
    )


async def test_the_bot_still_starts_a_dynamic_strategy_with_a_working_feed(
    created_strategies, feed_env
):
    """(e) A working feed must NOT be refused -- no false positive.

    A working feed is a host the OPERATOR controls that publishes the bot's
    "active_pairs_update" message.  It is NOT this fork's own frontend
    socket: that route is token-authenticated, speaks the frontend's
    messages, and has no publisher of "active_pairs_update" in this
    repository -- so it can never deliver a symbol (measured).
    """
    feed_env(
        source_mode="MAIN_APP", ws_enabled=True, ws_url=OPERATOR_PUBLISHER_WS_URL
    )

    controller = _controller()

    await _deliver(controller, _start_command(symbol_selection_mode="DYNAMIC"))

    assert created_strategies == ["VisualBuilderStrategy"], (
        "a DYNAMIC strategy with a working feed must still be started: "
        f"refusing it would be a false positive. Got {created_strategies!r}."
    )


async def test_the_bot_still_starts_a_static_strategy_with_a_broken_feed(
    created_strategies, feed_env
):
    """A strategy with explicit symbols needs no feed."""
    feed_env(source_mode="MAIN_APP", ws_enabled=True, ws_url="")

    controller = _controller()

    await _deliver(controller, _start_command(symbol_selection_mode="STATIC"))

    assert created_strategies == ["VisualBuilderStrategy"], (
        "a strategy with explicit symbols must still be started on a "
        f"deployment whose screener feed is broken. Got {created_strategies!r}."
    )


async def test_the_bot_still_starts_a_dynamic_strategy_in_static_list_mode(
    created_strategies, feed_env
):
    """STATIC_LIST populates the symbols without any WebSocket."""
    feed_env(source_mode="STATIC_LIST", ws_enabled=False, ws_url="")

    controller = _controller()

    await _deliver(controller, _start_command(symbol_selection_mode="DYNAMIC"))

    assert created_strategies == ["VisualBuilderStrategy"], (
        "in STATIC_LIST mode the symbol list is read from the config, so a "
        f"DYNAMIC strategy is startable. Got {created_strategies!r}."
    )

# ---------------------------------------------------------------------------
# 3b: the Origin header the screener client sends.
# ---------------------------------------------------------------------------


def test_the_origin_header_names_the_host_the_bot_actually_connects_to():
    """The Origin a browser would send for the fork's own screener."""
    from bot_module import screener_feed

    assert screener_feed.ws_origin_header(FORK_OWN_WS_URL) == (
        "https://depthsight.diverseinc.net"
    )


def test_the_origin_header_is_never_the_upstream_hostname():
    """The upstream's reserved hostname must not be sent by this fork."""
    from bot_module import screener_feed

    origin = screener_feed.ws_origin_header(FORK_OWN_WS_URL)
    assert "depthsight.pro" not in origin, (
        "the Origin header still names the UPSTREAM's reserved hostname: "
        f"{origin!r}"
    )


def test_the_origin_header_follows_the_configured_feed_host():
    """Point the feed at another host and the Origin follows it."""
    from bot_module import screener_feed

    assert screener_feed.ws_origin_header("wss://screener.example.com/ws/bot/") == (
        "https://screener.example.com"
    )
    assert screener_feed.ws_origin_header("ws://localhost:8765/ws") == (
        "http://localhost:8765"
    )
    # A DEFAULT port is left off, exactly as a browser omits it.
    assert screener_feed.ws_origin_header("wss://screener.example.com:443/ws") == (
        "https://screener.example.com"
    )
    assert screener_feed.ws_origin_header("ws://localhost:80/ws") == (
        "http://localhost"
    )
    # An IPv6 literal has to keep its brackets, or the Origin is not a URL.
    assert screener_feed.ws_origin_header("wss://[::1]:8765/ws") == (
        "https://[::1]:8765"
    )


def test_the_origin_header_falls_back_to_this_fork_own_domain():
    """An unusable URL still never yields the upstream's hostname."""
    from bot_module import screener_feed

    for bad in ("", "   ", "not a url", "https://example.com/x"):
        assert screener_feed.ws_origin_header(bad) == (
            "https://depthsight.diverseinc.net"
        ), f"bad URL {bad!r} produced the wrong Origin"


def test_the_screener_client_derives_its_origin_instead_of_hardcoding_it():
    """The client's Origin must be DERIVED from MAIN_APP_WS_URL, not literal."""
    source = (REPO_ROOT / "bot_module" / "data_consumer.py").read_text(
        encoding="utf-8"
    )
    assert "screener.depthsight.pro" not in source, (
        "bot_module/data_consumer.py still hardcodes the UPSTREAM's reserved "
        "hostname"
    )

    tree = ast.parse(source)
    origins = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", None) == "extra_headers" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if getattr(key, "value", None) == "Origin":
                origins.append(value)

    assert origins, "the extra_headers dict must set an Origin header"
    for value in origins:
        assert isinstance(value, ast.Call), (
            "the Origin header must be a CALL that derives the host, not a "
            f"literal: {ast.dump(value)}"
        )
        assert getattr(value.func, "id", None) == "ws_origin_header", (
            "the Origin header must be derived by ws_origin_header(): "
            f"{ast.dump(value)}"
        )
        arg = value.args[0] if value.args else None
        assert (
            isinstance(arg, ast.Attribute)
            and arg.attr == "_main_app_ws_url"
        ), (
            "the Origin header must be derived from the CONFIGURED feed "
            "URL (self._main_app_ws_url), not from a literal or from "
            "some other URL: "
            f"{ast.dump(value)}"
        )


def test_the_origin_header_never_names_the_upstream_even_when_it_is_configured():
    """Even pointed at the upstream, the Origin must not be the upstream's.

    The feed guard refuses that configuration, so it cannot reach the wire
    today.  The property is kept unconditional anyway: an Origin naming the
    upstream's reserved hostname is the one thing that would make the
    upstream answer this fork.
    """
    from bot_module import screener_feed

    assert screener_feed.ws_origin_header("wss://screener.depthsight.pro/ws/bot/") == (
        "https://depthsight.diverseinc.net"
    )
