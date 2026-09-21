# api/routes/market_data_health.py
#
# FIX 2026-09-20: per-stream candle-flow health endpoint.
#
# The bot's data_consumer maintains two pieces of Redis state:
#   1. A GLOBAL SET  market_data:active_streams  -> { stream_key, ... }
#      where stream_key = "{exchange}:{market_type}:{uc_symbol}@kline_{tf}"
#      Members are added on local subscribe, removed on unsubscribe.
#   2. PER-STREAM HEARTBEAT KEYS  market_data:candle_received:{exchange}:{market_type}:{uc_symbol}:{timeframe}
#      value: epoch ms; TTL: MARKET_DATA_CANDLE_HEALTH_TTL_SECONDS (default 600s)
#      Written on every kline receive (open OR close) by _update_local_cache.
#
# This endpoint:
#   - Reads the active streams SET to enumerate currently-subscribed streams
#   - Joins against the user's running strategies to annotate owner
#   - MGETs the heartbeat keys to compute "live" / "stale" / "silent" / "unknown"
#
# The dashboard uses these to render a per-strategy candle-flow indicator so
# users can tell at a glance whether their bots are receiving data. Without
# this, "is it actually working?" is guesswork.

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Query

from .. import models, schemas
from ..dependencies import get_current_user
from ..redis_client import get_redis_client
from bot_module import config as bot_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market-data", tags=["market-data"])


# Parses stream_key = "{exchange}:{market_type}:{sym_lowercase}@kline_{tf}"
# into (exchange, market_type, uc_symbol, timeframe). Returns None if shape
# doesn't match (depth / aggTrade / openInterest streams are ignored — this
# endpoint surfaces kline flows only).
_KLINE_STREAM_RE = re.compile(
    r"^(?P<exchange>[^:]+):(?P<market_type>[^:]+):(?P<symbol>[^@]+)@kline_(?P<tf>[^:]+)$"
)


def _parse_stream_key(stream_key: str) -> Optional[Dict[str, str]]:
    if not isinstance(stream_key, str):
        return None
    m = _KLINE_STREAM_RE.match(stream_key.strip())
    if not m:
        return None
    return {
        "exchange": m.group("exchange"),
        "market_type": m.group("market_type"),
        "symbol": m.group("symbol").upper(),
        "timeframe": m.group("tf"),
    }


def _classify(seconds: Optional[float], timeframe: str) -> str:
    """Map seconds-since-last-candle to live/stale/silent/unknown.

    Thresholds scale with timeframe so a 1m stream is judged by seconds while
    a 4h stream is judged by hours. This is a UX indicator, not a circuit breaker.
    """
    if seconds is None:
        return "unknown"
    try:
        unit = timeframe[-1]
        num = int(timeframe[:-1])
        if unit == "m":
            tf_secs = num * 60
        elif unit == "h":
            tf_secs = num * 3600
        elif unit == "d":
            tf_secs = num * 86400
        else:
            tf_secs = 60
    except (ValueError, IndexError):
        tf_secs = 60
    if seconds <= tf_secs * 1.5:
        return "live"
    if seconds <= tf_secs * 5:
        return "stale"
    return "silent"


def _split_symbols(s: str) -> List[str]:
    """Static-mode strategies store symbols as "BTCUSDT, ETHUSDT" or
    'Dynamic (All)'. Return a clean list of UC symbols when possible.
    """
    if not isinstance(s, str):
        return []
    if "Dynamic" in s:
        return []
    parts = [p.strip().upper().replace("/", "").replace(":", "") for p in s.split(",")]
    return [p for p in parts if p]


@router.get("/candle-health", response_model=schemas.CandleHealthResponse)
async def get_candle_health(
    mode: str = Query("paper", enum=["live", "paper"]),
    redis_client: redis.Redis = Depends(get_redis_client),
    current_user: models.User = Depends(get_current_user),
):
    """Return candle-flow health for the user's active streams."""
    now_ms = int(time.time() * 1000)
    entries: List[schemas.CandleHealthEntry] = []

    try:
        # 1. Read the GLOBAL active-streams set to learn what's subscribed.
        raw_streams = await redis_client.smembers(
            bot_config.MARKET_DATA_ACTIVE_STREAMS_SET_KEY
        )
        active_streams: List[Dict[str, str]] = []
        for sk in raw_streams or []:
            parsed = _parse_stream_key(sk)
            if parsed:
                active_streams.append(parsed)

        # 2. Build a lookup of (user-relevant) symbols per user — read their
        #    running strategies so we can tag entries with strategy_name.
        #    NOTE: the controller publishes market_type as "futures" / "spot"
        #    while stream_keys encode it as "futures_usdtm" / "spot" / etc.
        #    Match loosely by symbol only — for static strategies we don't
        #    need to nail the exact market_type since the dashboard only
        #    cares about "is data flowing for this user's symbols".
        user_symbol_to_strategy: Dict[str, Dict[str, Any]] = {}
        base_strategies_key = (
            f"{bot_config.REDIS_STATE_KEY_STRATEGIES}:{current_user.id}"
        )
        pattern = f"{base_strategies_key}:*"
        keys = await redis_client.keys(pattern)
        if keys:
            values = await redis_client.mget(keys)
            for raw in values:
                if not raw:
                    continue
                try:
                    arr = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                for strat in arr or []:
                    if not isinstance(strat, dict):
                        continue
                    if strat.get("mode") != mode:
                        continue
                    if str(strat.get("user_id")) != str(current_user.id):
                        continue
                    syms = _split_symbols(strat.get("symbol", ""))
                    owner = {
                        "id": str(strat.get("id")),
                        "name": strat.get("name")
                        or strat.get("strategy_name"),
                    }
                    for s in syms:
                        # First strategy wins per symbol — keeps the chip
                        # labeled with the owning strategy on the dashboard.
                        user_symbol_to_strategy.setdefault(s, owner)

        # 3. Filter active streams to ones that are relevant to this user.
        #    A stream is "relevant" if its symbol matches one of the user's
        #    running strategies (DYNAMIC-mode strategies don't have a
        #    concrete symbol list so they won't match anything here — we'll
        #    still show all active streams below if no static matches).
        relevant: List[Tuple[Dict[str, str], Optional[Dict[str, Any]]]] = []
        for parsed in active_streams:
            sym = parsed["symbol"]
            owner = user_symbol_to_strategy.get(sym)
            if owner:
                relevant.append((parsed, owner))

        # If we couldn't match any stream against user symbols but there
        # are active streams, surface them all (anonymous). This covers:
        #  - dynamic-mode strategies where symbol list is "Dynamic (All)"
        #  - users whose running strategy list briefly emptied
        if not relevant and active_streams:
            relevant = [(p, None) for p in active_streams]

        # 4. MGET heartbeats for relevant streams.
        if relevant:
            redis_keys = [
                (
                    f"{bot_config.MARKET_DATA_CANDLE_HEALTH_KEY_PREFIX}:"
                    f"{p['exchange']}:{p['market_type']}:{p['symbol']}:{p['timeframe']}"
                )
                for p, _ in relevant
            ]
            raw_values = await redis_client.mget(redis_keys)
            for (parsed, owner), raw in zip(relevant, raw_values):
                last_ts_ms: Optional[int] = None
                seconds: Optional[float] = None
                if raw:
                    try:
                        last_ts_ms = int(raw)
                        seconds = max(0.0, (now_ms - last_ts_ms) / 1000.0)
                    except (TypeError, ValueError):
                        pass
                entries.append(
                    schemas.CandleHealthEntry(
                        strategy_id=(owner or {}).get("id"),
                        strategy_name=(owner or {}).get("name"),
                        exchange=parsed["exchange"],
                        market_type=parsed["market_type"],
                        symbol=parsed["symbol"],
                        timeframe=parsed["timeframe"],
                        last_candle_ts_ms=last_ts_ms,
                        seconds_since_last_candle=(
                            round(seconds, 1) if seconds is not None else None
                        ),
                        status=_classify(seconds, parsed["timeframe"]),
                    )
                )

        # Sort: live first, then stale, then silent, then unknown; tie-break
        # by symbol then timeframe.
        _ORDER = {"live": 0, "stale": 1, "silent": 2, "unknown": 3}
        entries.sort(
            key=lambda e: (
                _ORDER.get(e.status, 9),
                e.symbol,
                e.timeframe,
            )
        )

    except Exception as e:
        logger.error(f"[/market-data/candle-health] failed: {e}", exc_info=True)

    return schemas.CandleHealthResponse(streams=entries, evaluated_at_ms=now_ms)
