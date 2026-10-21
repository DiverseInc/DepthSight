"""Can THIS deployment deliver the regime data the regime-change stop needs?

WHY THIS MODULE EXISTS
----------------------
`breakeven_on_regime_change` is implemented in `check_on_screener_update`
(bot_module/strategy.py:3070-3079). Its only production caller is
`_dynamic_symbol_selection_loop` in bot_module/controller.py -- the
`check_on_screener_update` call site is controller.py:5213 -- and that loop is
fed by the controller's screener-update queue. The only producer of that queue
is the `active_pairs_update` message on the main_app WebSocket
(bot_module/data_consumer.py:3428).

So a strategy can arm a stop that this deployment can never deliver: the
strategy starts, reports "running", and the stop is inert from the first
second -- while the user believes they are protected.

TWO PATHS START A STRATEGY, AND BOTH NEED THE SAME ANSWER
-----------------------------------------------------------
1. HTTP -- `POST /api/v1/strategies` (api/routes/strategies.py). Guarded with
   a 503 since PR #28.
2. Redis -- the bot's `START_STRATEGY` command channel, handled by
   `TradingController._handle_start_strategy_command` (bot_module/controller.py).

The two answers MUST be identical, or the guard means nothing: a path that
refuses LESS than the other is a hole, and a path that refuses MORE is a false
positive that stops a protection which WOULD have worked (worse than the hole
it closes). So the predicate lives HERE, once, and both paths import it.

This module lives in `bot_module/` rather than `api/` because the dependency
already runs that way -- the API imports bot_module (api/crud.py:21,
api/schemas.py:17) -- and the bot process must not have to import FastAPI to
ask one question about its own configuration.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from bot_module import config as bot_config
except ImportError:  # pragma: no cover - the bot package is always present
    # The bot's config module could not be imported in this process. There is
    # then no screener WebSocket feed here either, so this deployment cannot
    # deliver the regime data. This is the same answer the API's own
    # MockConfig fallback produced before this module existed.
    bot_config = None  # type: ignore[assignment]

__all__ = [
    "arms_breakeven_on_regime_change",
    "screener_feed_can_deliver_regime_data",
]


def arms_breakeven_on_regime_change(
    config_data: Any, request_params: Optional[Dict[str, Any]]
) -> bool:
    """True when this launch would ARM `breakeven_on_regime_change`.

    The option lives at the TOP LEVEL of the strategy's config_data. That is
    where the schema puts it (api/schemas.py:1666), where the frontend writes
    it (frontend/src/pages/Strategies.tsx:650) and where the bot reads it:
    the controller copies config_data into the strategy instance's params
    (controller.py:2332-2333) and `_get_param` finds it at the top level
    (strategy.py:2387-2393).

    A launch can also override it through `params`, which the API merges into
    config_data ("Apply Dynamic Overrides from Request").

    Never raises. A malformed config must not become a 500 here, and it must
    not be read as "armed" either.
    """
    effective: Dict[str, Any] = {}
    if isinstance(config_data, dict):
        effective.update(config_data)
    elif isinstance(config_data, str):
        try:
            decoded = json.loads(config_data)
        except (TypeError, ValueError):
            return False
        if isinstance(decoded, dict):
            effective.update(decoded)
    if isinstance(request_params, dict):
        effective.update(request_params)
    return bool(effective.get("breakeven_on_regime_change"))


def screener_feed_can_deliver_regime_data() -> bool:
    """True when this deployment's screener feed can deliver regime data.

    DERIVED FROM THE REAL CODE PATH -- read this before widening it.

    Regime data arrives as `active_pairs_update` messages on the main_app
    WebSocket. data_consumer.py:3428 is the only place that puts them on the
    controller's screener-update queue, and that socket only exists when
    data_consumer.py:453-526 creates the task, which needs ALL THREE of:

      1. SYMBOL_SOURCE_MODE == "MAIN_APP"   (data_consumer.py:453, 456)
      2. MAIN_APP_WS_ENABLED is true         (data_consumer.py:463)
      3. MAIN_APP_WS_URL is a ws:// / wss:// URL
                                            (data_consumer.py:519-522)

    (The `websockets` import at data_consumer.py:520 is a packaging
    invariant -- `websockets>=10.0,<15.0` is a hard requirement in
    requirements.txt -- not a deployment setting, so it is not part of the
    predicate.)

    This is a CONFIGURATION test, not a reachability test. MAIN_APP_WS_URL
    has a code default (config.py:515) and nothing here can tell whether that
    host is answering. The honest claim is "this deployment cannot deliver
    the regime data", NOT "the feed is down".

    It is deliberately NOT broader than the feed's own condition: a
    deployment that satisfies all three keeps the protection, so this guard
    can never refuse a stop that would have worked.
    """
    if bot_config is None:  # pragma: no cover - see the import guard above
        return False
    source_mode = str(
        getattr(bot_config, "SYMBOL_SOURCE_MODE", "MAIN_APP")
    ).strip().upper()
    if source_mode != "MAIN_APP":
        return False
    if not getattr(bot_config, "MAIN_APP_WS_ENABLED", True):
        return False
    ws_url = getattr(bot_config, "MAIN_APP_WS_URL", "")
    if not isinstance(ws_url, str) or not ws_url:
        return False
    return ws_url.startswith(("ws://", "wss://"))
