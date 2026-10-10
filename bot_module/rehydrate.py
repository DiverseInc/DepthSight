"""
Rehydrate support: keep a cached START_STRATEGY payload in sync with Postgres.

Why this exists
---------------
The API caches the full START_STRATEGY payload in Redis at start time
(`running_strategy_payload:{user_id}:{config_id}`) so the bot can replay it on
restart without a DB round-trip. Both rehydrate layers replay that cached
payload verbatim:

    controller.TradingController._rehydrate_running_strategies_from_redis
    bot_runner._rehydrate_all_strategies_globally

That snapshot is a point-in-time copy. If `strategy_configs.config_data` is
edited afterwards -- through the editor, or by a maintenance script that writes
SQL directly -- the running strategy keeps the old config AND the next bot
restart replays the old one.

Observed live 2026-10-10: a `blocks` -> `entryConditions` conversion was applied
to two paper strategies and confirmed `running` with `status_detail: null`. A
bot restart ~90 seconds later republished the pre-conversion snapshot and both
strategies were back to `cannot_trade` on the legacy format. The Postgres rows
were correct the whole time. The API DELETE+POST remediation in the "config
change did not take effect" runbook works, but it is not durable: the next bot
restart reverts it.

The fix is to re-read `config_data` from Postgres during rehydrate instead of
trusting the cached copy, and to write the refreshed payload back to Redis so
later layers and later restarts begin from the corrected copy.

Design rules
------------
- Never raise. A config lookup failure degrades to the cached payload instead of
  aborting the rehydrate. Losing every strategy on restart is a far worse
  outcome than starting one with a slightly stale config.
- Refresh `config_data` only. `symbols` / `symbol_selection_mode` are launch-time
  decisions; silently swapping them would leave the strategy evaluating one
  instrument while subscribed to another.
- Symbol drift is refused, not applied. If the fresh config names a different
  symbol than the cached payload pins, rehydrate keeps the cached body entirely
  and logs. The cached payload is internally consistent; a half-updated one is
  not. Applying a symbol change is an API restart, which re-derives the config
  and the subscription together.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from bot_module.runtime_dependencies import crud

logger = logging.getLogger(__name__)

__all__ = ["refresh_payload_config_from_db"]


def _coerce_config_dict(raw: Any) -> Optional[Dict[str, Any]]:
    """
    Best-effort coercion of the `config_data` JSON column to a dict.

    The column is SQLAlchemy `JSON`, so Postgres hands back a dict. The string
    branch exists because a hand-written or legacy row can arrive as JSON text.
    Returns None rather than raising: the caller treats that as "keep the
    cached copy".
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


def _extract_config_id(payload: Dict[str, Any]) -> Optional[str]:
    """Both `config_id` and `id` are published; prefer the explicit one."""
    for key in ("config_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _symbol_of(config_data: Any) -> str:
    if isinstance(config_data, dict):
        return str(config_data.get("symbol") or "").strip().upper()
    return ""


async def refresh_payload_config_from_db(
    payload: Dict[str, Any],
    user_id: Any,
    db_session_factory: Callable[[], Any],
) -> Tuple[Dict[str, Any], bool]:
    """
    Return `(payload, refreshed)`.

    `payload` is the cached START_STRATEGY body read from Redis. If Postgres
    holds a different `config_data` for this config_id, the returned payload
    carries the current copy and `refreshed` is True; otherwise the original
    payload object is returned untouched with `refreshed` False.

    `user_id` arrives from a Redis key segment and is not guaranteed to be an
    int, so it is coerced inside the guard below rather than by the caller.

    Never raises.
    """
    config_id = _extract_config_id(payload)
    if not config_id:
        logger.warning(
            "[RehydrateConfigRefresh] Cached payload carries no config id; "
            "keeping the cached config_data verbatim."
        )
        return payload, False

    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        logger.warning(
            "[RehydrateConfigRefresh] Non-numeric user id %r for config_id=%s; "
            "keeping the cached config_data verbatim.",
            user_id,
            config_id,
        )
        return payload, False

    try:
        row = None
        async for db in db_session_factory():
            row = await crud.get_strategy_config(
                db, user_id=user_id_int, config_id=config_id
            )
            break

        if row is None:
            # Config deleted, or owned by a different user than the key claims.
            # Neither is a reason to skip the strategy, so keep the cached copy.
            logger.warning(
                "[RehydrateConfigRefresh] No Postgres row for config_id=%s "
                "user_id=%s; keeping the cached config_data verbatim.",
                config_id,
                user_id_int,
            )
            return payload, False

        fresh = _coerce_config_dict(getattr(row, "config_data", None))
        if fresh is None:
            logger.warning(
                "[RehydrateConfigRefresh] Could not read config_data for "
                "config_id=%s from Postgres; keeping the cached copy verbatim.",
                config_id,
            )
            return payload, False

        cached = payload.get("config_data")
        if cached == fresh:
            return payload, False

        cached_symbol = _symbol_of(cached)
        fresh_symbol = _symbol_of(fresh)
        if cached_symbol and fresh_symbol and cached_symbol != fresh_symbol:
            # REFUSE the swap. Swapping only config_data here would leave the
            # payload's own subscription fields (symbols / symbol_selection_mode)
            # describing the old instrument while config_data pins the new one --
            # a mismatch the caller has no way to resolve. The cached payload is
            # at least internally consistent. Restarting through the API
            # re-derives both halves together.
            logger.warning(
                "[RehydrateConfigRefresh] config_data symbol changed %s -> %s "
                "for config_id=%s. REFUSING to refresh: the cached payload's "
                "subscription fields still describe %s, and swapping config alone "
                "would evaluate one instrument's conditions against another's "
                "stream. Restart this strategy through the API to apply it.",
                cached_symbol,
                fresh_symbol,
                config_id,
                cached_symbol,
            )
            return payload, False

        updated = dict(payload)
        updated["config_data"] = fresh
        logger.warning(
            "[RehydrateConfigRefresh] config_data for config_id=%s was STALE in "
            "Redis; replaying the current Postgres copy instead. A restart "
            "re-published an outdated config.",
            config_id,
        )
        return updated, True
    except Exception as exc:  # noqa: BLE001 - rehydrate must never abort
        logger.warning(
            "[RehydrateConfigRefresh] Config refresh failed for config_id=%s "
            "(%s); keeping the cached config_data verbatim.",
            config_id,
            exc,
            exc_info=True,
        )
        return payload, False