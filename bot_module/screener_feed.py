"""Is the screener/symbol feed CONFIGURED correctly for THIS deployment?

WHY THIS MODULE EXISTS
-----------------------
This repository is a FORK of ``DepthSight-Pro/DepthSight``.  The fork
inherited the upstream's screener WebSocket URL as a HARDCODED default::

    MAIN_APP_WS_URL = "wss://screener.depthsight.pro/ws/bot/"

``screener.depthsight.pro`` is the UPSTREAM's reserved hostname.  This fork
never controlled that DNS, the upstream's deployment does not serve this fork's
symbols, and the hostname answers HTTP 525 (measured 2026-11-02).

Four facts make that default unusable here, and none of them are guesses:

1. The fork's own deployment is ``depthsight.diverseinc.net``.  Its
   ``caddy.config`` has exactly ONE host block, and the public screener page
   answers 200 on that domain.
2. The fork's WebSocket route is ``/ws`` (``api/websocket_server.py:221``),
   with ``/ws/main`` as an alias (``:390``).  There is NO ``/ws/bot/`` route in
   this repository at all -- that is the upstream's path.
3. ``filtered_pairs:update`` (``MAIN_APP_SYMBOL_UPDATE_TOPIC``) is a DEAD
   reference here: the producer was the upstream's separate screener service,
   which this fork does not run.
4. That ``/ws`` route is the FRONTEND's socket, not the bot's symbol feed.
   ``api/websocket_server.py:228-230`` closes every connection that carries
   no ``?token=``, and the bot's screener client sends no token
   (``bot_module/data_consumer.py:3414-3437``).  Even with a token the
   shapes disagree: the server forwards Redis messages as ``{"topic": ...,
   "payload": ...}`` (``:187-191``) while the bot only understands
   ``{"type": "active_pairs_update", "data": [...]}``
   (``data_consumer.py:3476-3478``).  So the URL this fork shipped in
   ``.env.example`` -- ``wss://depthsight.diverseinc.net/ws/main`` -- cannot
   deliver a symbol to the bot, and a DYNAMIC strategy pointed at it would
   report "running" and never trade.

So the inherited default could only ever produce a reconnect storm against a
host that can never work, while a DYNAMIC-mode strategy started, reported
"running", and silently never traded: no symbols to subscribe to, none to
match against.  That is the worst failure mode for a product, because it
looks healthy.

THE RULE (the vault's own, applied here)
----------------------------------------
``Projects/DepthSight - 2026-09-25 Session - Canonical Domain Swap + MiniMax
Marketing Page.md:96`` records the rule that was applied to ``PUBLIC_BASE_URL``
and MISSED here:

    "PUBLIC_BASE_URL defaults in source are a footgun when forking from
     upstream. ... Rule for future forks: set PUBLIC_BASE_URL to an empty
     default and require .env -- or assert at startup that PUBLIC_BASE_URL is
     reachable."

This module is that missing assertion, for ``MAIN_APP_WS_URL``.

ONE ANSWER, ASKED IN ONE PLACE
-------------------------------
Three different things can start with this configuration: the API route that
launches a strategy (``POST /api/v1/strategies``), the bot's Redis command
channel that starts a strategy (``_handle_start_strategy_command``), and the
consumer that starts the feed (``DataConsumer.start``).  They all ask THIS
module, so they cannot drift apart -- the same rule ``bot_module/
regime_feed.py`` already follows for the regime-change stop.

FAIL CLOSED
-----------
Every function here either returns ``None`` ("there is no problem") or returns
a message that says what is wrong AND what to set.  Every rule here makes a
deployment STRICTER than it was, never more permissive: a DYNAMIC-mode
strategy must never be reported "running" against a feed that cannot deliver
symbols.
"""

from __future__ import annotations

import logging
from typing import Any, FrozenSet, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

__all__ = [
    "UPSTREAM_DEPTHSIGHT_HOSTS",
    "FORK_OWN_DOMAIN",
    "FORK_OWN_WS_PATHS",
    "ws_url_host",
    "ws_url_path",
    "is_upstream_depthsight_url",
    "is_fork_own_frontend_ws_url",
    "describe_ws_url_problem",
    "screener_feed_misconfigured",
    "ws_origin_header",
]


# The upstream project's reserved hostnames.
#
# EXACT hostnames only, never a suffix or a substring: the owner may run this
# fork on ANY host they control, including one whose name merely ends with
# "depthsight.pro".  A suffix test would refuse that legitimate deployment, and
# a substring test would also refuse "depthsight.pro.example.invalid".
UPSTREAM_DEPTHSIGHT_HOSTS: FrozenSet[str] = frozenset(
    {
        "screener.depthsight.pro",
        "app.depthsight.pro",
        "depthsight.pro",
    }
)

# This fork's own public domain.  Named in the messages so the operator has
# something concrete to point the feed at.
FORK_OWN_DOMAIN = "depthsight.diverseinc.net"

# This fork's own FRONTEND WebSocket routes.
#
# api/websocket_server.py:221 serves "/ws" and :390 serves "/ws/main" as an
# ALIAS of it.  Neither can deliver a symbol to the bot:
#
#   * :228-230 closes every connection that carries no "?token=", and the
#     bot's screener client sends no token (data_consumer.py:3414-3437);
#   * :187-191 forwards Redis messages as {"topic": ..., "payload": ...},
#     while the bot only understands {"type": "active_pairs_update",
#     "data": [...]} (data_consumer.py:3476-3478);
#   * and nothing in this repository publishes "active_pairs_update" at all
#     -- the producer was the upstream's separate screener service, which
#     this fork does not run.
#
# So a feed URL pointing here is a DEAD feed, and this fork used to SHIP one:
# .env.example pointed MAIN_APP_WS_URL at
# wss://depthsight.diverseinc.net/ws/main.
#
# EXACT paths on THIS fork's own host only: an operator who runs their own
# screener service on their own host, on any path, must not be refused by
# this.  A path this fork does not serve is left alone for the same reason.
FORK_OWN_WS_PATHS: FrozenSet[str] = frozenset({"/ws", "/ws/main"})


# ---------------------------------------------------------------- pure helpers


def ws_url_host(url: Any) -> str:
    """The exact hostname of a WebSocket URL, lower-cased.  ``""`` if there is none.

    Pure.  ``urlsplit`` gives the hostname without the port and without the
    userinfo, and lower-casing makes the host comparison case-insensitive the
    way DNS is.  A trailing dot is stripped because "screener.depthsight.pro."
    is the same host as "screener.depthsight.pro".
    """
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        # A malformed URL is not a host.  Never raises: this runs at startup.
        return ""
    host = parsed.hostname or ""
    return host.strip().lower().rstrip(".")


def ws_origin_header(url: Any) -> str:
    """The ``Origin`` header to send when connecting to this WebSocket URL.

    The Origin has to name the host the client is ACTUALLY talking to.  A
    browser sends the origin of the page it is on, so a server (or a CDN in
    front of it) that compares Origin against its own host sees a request
    that claims to come from somewhere else.

    The fork used to send a HARDCODED ``https://screener.depthsight.pro`` --
    the UPSTREAM's reserved hostname -- no matter where the bot connected.
    So it pointed at a deployment this fork does not control, and it
    disagreed with the host in MAIN_APP_WS_URL.

    The scheme is mapped the way a browser maps it (``ws://`` is ``http://``,
    ``wss://`` is ``https://``) and the port is included only when it is not
    the default for that scheme.

    A URL that is not a WebSocket URL, that carries no usable host, or that
    names the UPSTREAM's reserved hostname falls back to THIS fork's own
    domain -- never to the upstream's.
    """
    fallback = f"https://{FORK_OWN_DOMAIN}"
    text = url.strip() if isinstance(url, str) else ""
    if not text:
        return fallback
    try:
        parsed = urlsplit(text)
    except ValueError:
        return fallback
    scheme = parsed.scheme.lower()
    if scheme not in ("ws", "wss"):
        return fallback
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return fallback
    if is_upstream_depthsight_url(text):
        # Never name the UPSTREAM's reserved hostname, not even when the
        # operator has pointed MAIN_APP_WS_URL at it.  The feed guard
        # refuses that configuration, so this is unreachable in practice --
        # but an Origin naming the upstream is the one thing that would
        # make the upstream answer this fork.  This fork's own domain is
        # the fail-closed answer: the upstream's CDN then rejects us.
        return fallback
    # An IPv6 literal has to stay BRACKETED, or the result is not a URL at all
    # ("https://::1:8765" does not parse).  A hostname never contains a colon,
    # so the colon is the discriminator.
    authority = f"[{host}]" if ":" in host else host
    http_scheme = "https" if scheme == "wss" else "http"
    default_port = 443 if http_scheme == "https" else 80
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and port != default_port:
        return f"{http_scheme}://{authority}:{port}"
    return f"{http_scheme}://{authority}"


def is_upstream_depthsight_url(url: Any) -> bool:
    """True when the URL points at the UPSTREAM DepthSight-Pro deployment.

    Exact host match only -- see ``UPSTREAM_DEPTHSIGHT_HOSTS``.
    """
    return ws_url_host(url) in UPSTREAM_DEPTHSIGHT_HOSTS


def ws_url_path(url: Any) -> str:
    """The PATH of a WebSocket URL: lower-cased, no trailing slash.

    Pure.  ``""`` when the URL names no path at all.  The trailing slash is
    dropped because ``/ws/main/`` is the same route as ``/ws/main`` for the
    purpose of "is this the fork's own frontend socket", and the case is
    dropped because a path is case-sensitive on the wire but this comparison
    only asks "is this one of the fork's own two routes".
    """
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        # A malformed URL is not a path.  Never raises: this runs at startup.
        return ""
    path = (parsed.path or "").strip()
    if not path:
        return ""
    return path.rstrip("/").lower()


def is_fork_own_frontend_ws_url(url: Any) -> bool:
    """True when the URL points at THIS fork's own frontend WebSocket route.

    That endpoint is the FRONTEND's socket -- it is token-authenticated, it
    speaks the frontend's ``{"topic", "payload"}`` messages, and nothing in
    this repository publishes the bot's ``active_pairs_update`` message -- so
    it can NEVER deliver a symbol to the bot.  A feed pointed at it is a dead
    feed, and a DYNAMIC strategy pointed at it would report "running" and
    never trade.

    The host must match EXACTLY and the path must be one of the fork's own
    two routes: an operator who runs their own screener service on their own
    host, on any path, must not be refused by this.
    """
    return (
        ws_url_host(url) == FORK_OWN_DOMAIN
        and ws_url_path(url) in FORK_OWN_WS_PATHS
    )


def _missing_url_message() -> str:
    return (
        "MAIN_APP_WS_URL is not set. This fork deliberately ships NO "
        "default screener URL, because the upstream DepthSight-Pro "
        "deployment's host is not this fork's deployment. Set "
        "MAIN_APP_WS_URL to the WebSocket URL of a screener service YOU "
        "control -- a host that publishes 'active_pairs_update' -- and "
        "switch the feed on with MAIN_APP_WS_ENABLED=true. Until then this "
        "deployment has NO symbol source in MAIN_APP mode: there is no "
        "automatic fallback to the static list, so a DYNAMIC-mode strategy "
        "would start and silently never trade."
    )


def _invalid_url_message(text: str) -> str:
    return (
        f"MAIN_APP_WS_URL ('{text}') is not a ws:// or wss:// URL, so it "
        "is not a WebSocket endpoint. The screener feed needs the "
        "WebSocket URL of a screener service you control -- a host that "
        "publishes 'active_pairs_update'."
    )


def _no_host_message(text: str) -> str:
    return (
        f"MAIN_APP_WS_URL ('{text}') is a ws:// or wss:// URL with NO "
        "HOST, so it names nothing to connect to. A WebSocket endpoint "
        "needs the host of a screener service you control -- a host that "
        "publishes 'active_pairs_update'."
    )


def _upstream_url_message(text: str, host: str) -> str:
    return (
        f"MAIN_APP_WS_URL ('{text}') points at the UPSTREAM DepthSight-Pro "
        f"deployment ('{host}'), NOT this fork. That hostname is the "
        "upstream's reserved name: this fork never controlled that DNS, it "
        "is not this fork's deployment, and it answers HTTP 525. Point "
        "MAIN_APP_WS_URL at a screener service you control -- a host that "
        "publishes 'active_pairs_update'. Do not point it at this fork's "
        f"own frontend socket (wss://{FORK_OWN_DOMAIN}/ws/main) either: "
        "that route is the frontend's token-authenticated WebSocket and it "
        "cannot deliver a symbol to the bot."
    )


def _fork_own_feed_message(text: str) -> str:
    return (
        f"MAIN_APP_WS_URL ('{text}') points at THIS fork's own frontend "
        f"WebSocket ({FORK_OWN_DOMAIN}/ws or /ws/main). That endpoint "
        "cannot deliver a symbol to the bot: it is the FRONTEND's socket, "
        "it closes every connection that carries no browser token (the bot "
        "sends none), it speaks the frontend's {'topic', 'payload'} "
        "messages, and nothing in this repository publishes the bot's "
        "'active_pairs_update' message at all. A DYNAMIC strategy pointed "
        "at it would report 'running' and never trade. Point MAIN_APP_WS_URL "
        "at a screener service you control -- a host that publishes "
        "'active_pairs_update' -- or launch the strategy with explicit "
        "symbols (STATIC/FIXED)."
    )


def _feed_disabled_message() -> str:
    return (
        "MAIN_APP_WS_ENABLED is false, so this deployment has NO screener "
        "feed running and NOTHING can deliver a symbol. There is no "
        "automatic fallback to the static list: a DYNAMIC-mode strategy "
        "started here would have ZERO symbols, report 'running' and never "
        "trade. Switch the feed on (MAIN_APP_WS_ENABLED=true) and point "
        "MAIN_APP_WS_URL at a screener service that publishes "
        "'active_pairs_update', or launch the strategy with explicit "
        "symbols (STATIC/FIXED)."
    )


def describe_ws_url_problem(url: Any) -> Optional[str]:
    """Why this WebSocket URL is unusable, or ``None`` when it is usable.

    Pure -- it takes the URL, not the config module, so the rules are testable
    without importing anything.

    The four bad states, in the order they are checked:

    * MISSING   -- empty.  This fork ships NO default on purpose (see the
      module docstring), so an empty value means "the operator has not
      configured the feed", and that must be NAMED, not defaulted away.
    * NOT-A-WS  -- the scheme is not ws:// or wss://, so the client is not
      talking to a WebSocket at all.
    * NO-HOST   -- the scheme IS ws:// or wss://, but the URL names NO
      HOST, so there is nothing to connect to.  The NOT-A-WS check is
      a SCHEME PREFIX test, so "ws://" and "ws:///ws/main" used to
      pass it.
    * UPSTREAM  -- the host is one of the upstream's reserved hostnames.
    """
    text = url.strip() if isinstance(url, str) else ""
    if not text:
        return _missing_url_message()
    if not text.lower().startswith(("ws://", "wss://")):
        return _invalid_url_message(text)
    if not ws_url_host(text):
        return _no_host_message(text)
    if is_upstream_depthsight_url(text):
        return _upstream_url_message(text, ws_url_host(text))
    return None


def screener_feed_misconfigured(
    source_mode: Any, ws_url: Any, ws_enabled: Any
) -> Optional[str]:
    """Why this deployment's screener feed CANNOT deliver a symbol, else ``None``.

    Pure.  "Wrong" means any of FOUR things:

    1. THE UPSTREAM POINTER -- the URL names the UPSTREAM DepthSight-Pro
       deployment.  That is wrong in ANY pairing: this fork never controlled
       that DNS, the host is not this fork's deployment, and it answers 525.

    2. THE FORK'S OWN FRONTEND SOCKET -- the URL names THIS fork's own
       frontend WebSocket route (``/ws`` or ``/ws/main``).  That endpoint is
       the FRONTEND's socket: it is token-authenticated, it speaks the
       frontend's ``{"topic", "payload"}`` messages, and nothing in this
       repository publishes the bot's ``active_pairs_update`` message.  So it
       is a DEAD feed in ANY pairing -- and it is the URL this fork used to
       ship in ``.env.example``.

    3. THE FEED IS OFF -- ``MAIN_APP_WS_ENABLED`` is false.  With the feed
       off there is NO symbol source at all.  The bot's DYNAMIC symbol pool
       is ``_last_known_symbols_from_consumer`` (controller.py:7216), and
       that set is filled ONLY by the feed's ``active_pairs_update`` message
       (data_consumer.py:3508 -> ``controller.start_managing_symbol``, whose
       only production caller is the feed).  There is no automatic fallback
       to the static list, so a DYNAMIC strategy started here has ZERO
       symbols: it reports "running" and never trades.

    4. THE UNSAFE PAIRING -- the feed is switched ON but its URL is missing
       or unusable (empty, hostless, or not a ws:// / wss:// URL).  The
       consumer refuses to start, so nothing would ever deliver symbols.

    A URL that IS set must be usable in BOTH pairings: it is the cause the
    moment the feed is switched on, and it is the wrong configuration right
    now.

    NOT wrong: anything at all when ``SYMBOL_SOURCE_MODE`` is not
    ``MAIN_APP`` -- a STATIC_LIST or JSON_FILE deployment gets its symbols
    from the config, with no WebSocket involved.

    This guard is asked ONLY about a DYNAMIC-mode start, so refusing here
    never touches a STATIC, FIXED or STATIC_LIST strategy.

    The CONSUMER keeps asking the narrower question -- "can I open a socket
    to this URL?", which is ``describe_ws_url_problem`` -- and that is
    deliberate.  A dead feed there produces a reconnect storm and the
    consumer's CRITICAL outage alarm, which is loud; refusing to start the
    whole consumer would take the MARKET DATA down over a screener
    misconfiguration.  The two rules answer two different questions, and
    this one is the stricter of the two where it counts: a strategy.
    """
    if str(source_mode or "").strip().upper() != "MAIN_APP":
        return None
    text = ws_url.strip() if isinstance(ws_url, str) else ""
    if is_upstream_depthsight_url(text):
        return _upstream_url_message(text, ws_url_host(text))
    if is_fork_own_frontend_ws_url(text):
        return _fork_own_feed_message(text)
    if not ws_enabled:
        # The feed is switched OFF: nothing can deliver a symbol at all.
        # A URL that is set but unusable is named first, because it is the
        # cause the moment the feed is switched on.
        if text:
            _url_problem = describe_ws_url_problem(text)
            if _url_problem is not None:
                return _url_problem
        return _feed_disabled_message()
    return describe_ws_url_problem(text)
