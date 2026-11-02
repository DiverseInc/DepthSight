"""Is the screener/symbol feed CONFIGURED correctly for THIS deployment?

WHY THIS MODULE EXISTS
-----------------------
This repository is a FORK of ``DepthSight-Pro/DepthSight``.  The fork
inherited the upstream's screener WebSocket URL as a HARDCODED default::

    MAIN_APP_WS_URL = "wss://screener.depthsight.pro/ws/bot/"

``screener.depthsight.pro`` is the UPSTREAM's reserved hostname.  This fork
never controlled that DNS, the upstream's deployment does not serve this fork's
symbols, and the hostname answers HTTP 525 (measured 2026-11-02).

Three facts make that default unusable here, and none of them are guesses:

1. The fork's own deployment is ``depthsight.diverseinc.net``.  Its
   ``caddy.config`` has exactly ONE host block, and the public screener page
   answers 200 on that domain.
2. The fork's WebSocket route is ``/ws`` (``api/websocket_server.py:221``),
   with ``/ws/main`` as an alias (``:390``).  There is NO ``/ws/bot/`` route in
   this repository at all -- that is the upstream's path.
3. ``filtered_pairs:update`` (``MAIN_APP_SYMBOL_UPDATE_TOPIC``) is a DEAD
   reference here: the producer was the upstream's separate screener service,
   which this fork does not run.

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
a message that says what is wrong AND what to set.  Nothing in here is allowed
to make a deployment MORE permissive than it was before.
"""

from __future__ import annotations

import logging
from typing import Any, FrozenSet, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

__all__ = [
    "UPSTREAM_DEPTHSIGHT_HOSTS",
    "FORK_OWN_DOMAIN",
    "ws_url_host",
    "is_upstream_depthsight_url",
    "describe_ws_url_problem",
    "screener_feed_misconfigured",
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


def is_upstream_depthsight_url(url: Any) -> bool:
    """True when the URL points at the UPSTREAM DepthSight-Pro deployment.

    Exact host match only -- see ``UPSTREAM_DEPTHSIGHT_HOSTS``.
    """
    return ws_url_host(url) in UPSTREAM_DEPTHSIGHT_HOSTS


def _missing_url_message() -> str:
    return (
        "MAIN_APP_WS_URL is not set. This fork deliberately ships NO "
        "default screener URL, because the upstream DepthSight-Pro "
        "deployment's host is not this fork's deployment. Set "
        "MAIN_APP_WS_URL to this fork's own screener WebSocket endpoint "
        f"(for example wss://{FORK_OWN_DOMAIN}/ws/main), or point it at "
        "your own screener. Until then this deployment has NO symbol "
        "source in MAIN_APP mode: there is no automatic fallback to the "
        "static list, so a DYNAMIC-mode strategy would start and silently "
        "never trade."
    )


def _invalid_url_message(text: str) -> str:
    return (
        f"MAIN_APP_WS_URL ('{text}') is not a ws:// or wss:// URL, so it "
        "is not a WebSocket endpoint. The screener feed needs the "
        "WebSocket URL of this fork's own screener service "
        f"(for example wss://{FORK_OWN_DOMAIN}/ws/main)."
    )


def _upstream_url_message(text: str, host: str) -> str:
    return (
        f"MAIN_APP_WS_URL ('{text}') points at the UPSTREAM DepthSight-Pro "
        f"deployment ('{host}'), NOT this fork. That hostname is the "
        "upstream's reserved name: this fork never controlled that DNS, it "
        "is not this fork's deployment, and it answers HTTP 525. Point "
        "MAIN_APP_WS_URL at this fork's own domain "
        f"(for example wss://{FORK_OWN_DOMAIN}/ws/main) or at your own "
        "screener."
    )


def describe_ws_url_problem(url: Any) -> Optional[str]:
    """Why this WebSocket URL is unusable, or ``None`` when it is usable.

    Pure -- it takes the URL, not the config module, so the rules are testable
    without importing anything.

    The three bad states, in the order they are checked:

    * MISSING   -- empty.  This fork ships NO default on purpose (see the
      module docstring), so an empty value means "the operator has not
      configured the feed", and that must be NAMED, not defaulted away.
    * NOT-A-WS  -- the scheme is not ws:// or wss://, so the client is not
      talking to a WebSocket at all.
    * UPSTREAM  -- the host is one of the upstream's reserved hostnames.
    """
    text = url.strip() if isinstance(url, str) else ""
    if not text:
        return _missing_url_message()
    if not text.lower().startswith(("ws://", "wss://")):
        return _invalid_url_message(text)
    if is_upstream_depthsight_url(text):
        return _upstream_url_message(text, ws_url_host(text))
    return None


def screener_feed_misconfigured(
    source_mode: Any, ws_url: Any, ws_enabled: Any
) -> Optional[str]:
    """Why the screener feed's CONFIGURATION is wrong here, else ``None``.

    Pure.  "Wrong" means one of TWO things:

    1. THE UNSAFE PAIRING -- the feed is switched ON but its URL is missing
       or unusable.  The consumer refuses to start, so nothing would ever
       deliver symbols, and a DYNAMIC-mode strategy would run with ZERO
       symbols: it reports "running" and never trades.  This pairing is the
       one the guard has to refuse, loudly, at startup.

    2. THE UPSTREAM POINTER -- the URL names the UPSTREAM DepthSight-Pro
       deployment.  That is wrong in ANY pairing: this fork never controlled
       that DNS, the host is not this fork's deployment, and it answers 525.

    NOT wrong: the feed switched OFF with an empty URL.  That is the SAFE
    pairing.  There is no feed to misconfigure, the consumer keeps its
    market data, and whether DYNAMIC symbol selection may be used at all is
    decided by the ESTABLISHED rules -- ``SYMBOL_SELECTION_ALLOW_DYNAMIC``
    (which defaults to false and already refuses a DYNAMIC start at the API
    with a 503) and the CRITICAL outage alarm in the consumer's
    disabled-feed branch.  Refusing a DYNAMIC start here as well would make
    the guard "always refuse everything", which is exactly what
    ``tests/test_safety_guard_regressions.py`` exists to prevent.
    """
    if str(source_mode or "").strip().upper() != "MAIN_APP":
        return None
    text = ws_url.strip() if isinstance(ws_url, str) else ""
    if is_upstream_depthsight_url(text):
        return _upstream_url_message(text, ws_url_host(text))
    if not ws_enabled:
        # The SAFE pairing: the feed is off and the URL is empty.
        return None
    return describe_ws_url_problem(text)
