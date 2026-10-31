import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .. import crud, models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..redis_client import get_redis_client
from ..plans import plans_config

try:
    from bot_module import config as bot_config
except ImportError:

    class MockConfig:
        REDIS_COMMAND_CHANNEL = "depthsight:commands"

    bot_config = MockConfig()

REDIS_COMMAND_CHANNEL = getattr(
    bot_config, "REDIS_COMMAND_CHANNEL", "depthsight:commands"
)
HFT_CMD_CHANNEL = "hft:commands"

logger = logging.getLogger(__name__)

config_router = APIRouter(
    prefix="/api/v1",
    tags=["Configuration"],
    dependencies=[Depends(get_current_user)],
)


@config_router.get(
    "/config",
    response_model=schemas.ApiResponseData[schemas.AppConfig],
    summary="Get current configuration",
)
async def get_config_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) fetching application configuration."
    )
    config = await crud.get_config(db, user_id=current_user.id)
    if not config:
        # Auto-recover: if a user (typically from an older Google OAuth signup
        # before create_oauth_user created the AppConfig) has no config, build
        # one on demand. Prevents the "Engine Room / Configuration for user X
        # not found" 404 that would otherwise brick the page.
        logger.warning(
            f"AppConfig for user '{current_user.username}' (ID: {current_user.id}) "
            f"not found — creating default config on demand."
        )
        try:
            await crud.create_default_config_for_existing_user(
                db, user_id=current_user.id
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to auto-create AppConfig for user {current_user.id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create default configuration.",
            )
        config = await crud.get_config(db, user_id=current_user.id)
        if not config:
            # Should not happen, but defensive
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve configuration after auto-create.",
            )
    return {"data": config}


def _refuse_a_risk_per_trade_above_the_floor(
    payload: Dict[str, Any], section: str
) -> None:
    """Refuse a per-trade risk the server will not honour.

    The RiskManager enforces the server's own floor on every trade: no
    single trade may risk more than ``DEFAULT_RISK_PER_TRADE_PERCENT`` of
    the balance (bot_module/config.py; the registry reads the same constant
    as ``SERVER_RISK_PER_TRADE_PCT``). A Risk Settings page that stored a
    larger number would display a limit the server does not honour.

    REFUSED, not clamped: a silently clamped number is a number the client
    did not ask for and does not see, and the page would keep showing the
    value it sent. The client is told the limit instead.

    The error shape is this file's own: an ``HTTPException`` with a
    ``detail``. The status is 422, the same code the strategy contract uses
    for the same class of refusal (a value the server will not accept).
    """
    value = payload.get("riskPerTradePercent")
    if value is None:
        return
    floor = float(
        getattr(bot_config, "DEFAULT_RISK_PER_TRADE_PERCENT", 1.0)
    )
    try:
        risk_per_trade = float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{section}.riskPerTradePercent must be a number.",
        )
    if risk_per_trade < 0.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{section}.riskPerTradePercent={risk_per_trade} is not a valid "
                f"risk per trade."
            ),
        )
    if risk_per_trade > floor:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{section}.riskPerTradePercent={risk_per_trade} is looser than "
                f"the server limit of {floor}% of the balance. The server "
                f"enforces that limit on every trade, so it cannot be raised here."
            ),
        )


def _the_fields_the_client_actually_sent(section_model: Any) -> Dict[str, Any]:
    """The fields the client actually sent -- and ONLY those.

    Pydantic fills in the schema default for every field the client omitted,
    so a dumped payload cannot tell "the client sent 1.0" from "the client
    sent nothing, and the default happens to be 1.0". MEASURED on
    ``RiskManagementSettings``: a client that sent three fields gets a dumped
    payload of eighteen, including ``riskPerTradePercent: 1.0`` that nobody
    asked for.

    That distinction decides TWO things, and getting it wrong breaks the
    product rather than merely looking wrong:

    * the GUARD must compare only what was sent. Otherwise a client that
      tightened its per-trade risk to 0.5 in the strategy editor is refused
      on the Risk Settings page for a ``1.0`` it never sent -- and the page
      can never be saved again, because the only way back is to raise the
      limit, which the guard itself forbids.
    * the WRITE must carry only what was sent. ``update_config_section``
      MERGES, but it merges every key it is handed, and an injected default
      is not None: so a field the client omitted would silently overwrite
      the tightened value the account already had.

    ``model_fields_set`` is the instrument: it holds the fields the client
    explicitly provided, and nothing else.
    """
    fields = type(section_model).model_fields
    sent: Dict[str, Any] = {}
    for name in section_model.model_fields_set:
        info = fields.get(name)
        if info is None:
            continue
        value = getattr(section_model, name, None)
        if value is None:
            continue
        sent[info.alias or name] = value
    return sent


def _refuse_a_risk_limit_looser_than_its_ceiling(
    payload: Dict[str, Any], section: str, current: Dict[str, Any]
) -> None:
    """Refuse a risk limit the server will not honour.

    FIVE account-level risk limits have a server floor, and the floors live in
    ONE place -- ``bot_module/strategy_registry.py``: ``SERVER_RISK_FLOOR`` is
    the limit and ``RISK_FIELD_TO_RM_KEY`` is the account key each one is
    stored under. The account key names ARE the schema's field names
    (``api/schemas.py`` ``RiskManagementSettings``), measured: the alias of
    ``maxDrawdown`` is ``maxDrawdown``, and so on for all five.

    WHAT THE FLOORS ACTUALLY ARE, measured against the block a new account is
    handed (``api/crud.py`` ``create_user``):

    | limit | declared floor | the account is handed | |
    |---|---|---|---|
    | ``risk_per_trade_pct`` | 1.0 % | 1.0 % | the same |
    | ``max_consecutive_losses`` | 10 | 10 | the same |
    | ``daily_loss_limit_pct`` | 5.0 % | *nothing* | the floor fills in |
    | ``max_drawdown_pct`` | 20.0 % | 10.0 % | the account is TIGHTER than the floor |
    | ``max_stop_distance_pct`` | 5.0 % | 10.0 % | the account is LOOSER than the floor |

    So "an account that has never changed one is exactly at its floor" is
    true for two of the five, and FALSE for the other three. The two that
    disagree are the platform's own inconsistency, and it is recorded for the
    owner rather than silently resolved here: changing a risk limit is the
    owner's decision, not this route's.

    The strategy route already refuses a limit looser than its floor. THIS
    route did not, so a client could raise four of the five through the Risk
    Settings page -- and the RiskManager enforces the ACCOUNT's stored value
    (bot_module/risk_manager.py:210-232 reads it and only falls back to the
    server constant when the key is absent), so the raised number really did
    govern.

    WHICH LIMITS ARE READ FROM WHICH BLOCK. The RiskManager reads
    ``maxDrawdown``, ``dailyMaxLossPercent`` and ``maxConsecutiveLosses``
    from the LIVE ``risk_management`` block only; the backtest block's
    copies of those three are dead data. It reads ``riskPerTradePercent``
    and ``maxStopDistancePct`` from the backtest block. Guarding all five in
    both sections is the conservative choice, but only the two above govern
    the paper account.

    A client may TIGHTEN a limit and never LOOSEN one, so the ceiling is the
    account's OWN current value where the account holds one, and the server
    floor where it holds none. The RiskManager enforces the account's stored
    number, so that stored number IS the limit -- it is not a suggestion the
    server quietly ignores. Where the account's own default is looser than the
    declared floor (the stop distance), the account's value wins: it is the
    platform's own default, it is what the RiskManager enforces, and refusing
    it would break the Risk Settings page for every client.

    ``payload`` here is ONLY the fields the client actually sent -- see
    ``_the_fields_the_client_actually_sent``. A value the client never sent
    must not be compared, and must not be written either.

    REFUSED, not clamped: a silently clamped number is a number the client
    did not ask for and does not see, and the page would keep showing the
    value it sent. The client is told the limit instead.

    ``position_size_pct`` has no account key of its own -- it is the position
    budget, carried by the strategy's own per-trade parameter -- so it is not
    in this set, and a save that carries it changes nothing here.
    """
    from bot_module.strategy_registry import RISK_FIELD_TO_RM_KEY, SERVER_RISK_FLOOR

    current = current or {}
    for field_name, rm_key in RISK_FIELD_TO_RM_KEY.items():
        value = payload.get(rm_key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{section}.{rm_key} must be a number.",
            )
        if number < 0.0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{section}.{rm_key}={number} is not a valid risk limit.",
            )
        ceiling = float(SERVER_RISK_FLOOR[field_name])
        current_value = current.get(rm_key)
        if current_value is not None:
            try:
                held = float(current_value)
            except (TypeError, ValueError):
                held = None
            if held is not None:
                # The account's OWN value is the ceiling: a client may TIGHTEN
                # a limit and never LOOSEN one, which is the same rule the
                # strategy route already applies
                # (bot_module/strategy_registry.py: ``apply_client_risk_config``
                # caps the request at the account's current value).
                #
                # Where the account already holds a value LOOSER than the
                # declared floor, that value wins: it is the platform's own
                # default, the RiskManager enforces it
                # (bot_module/risk_manager.py:250-257 reads the account's
                # value), and refusing it would change a limit the platform
                # handed out and break the Risk Settings page for every
                # client. The floor still applies to a field the account has
                # no value for.
                #
                # NOTE (2026-10-31, round 21): the platform declares a 5%
                # maximum stop distance (bot_module/strategy_registry.py:
                # SERVER_MAX_STOP_DISTANCE_PCT) but hands out 10% as the
                # account default, and the RiskManager's own fallback is 2%
                # (bot_module/config.py: RISK_MANAGER_MAX_STOP_DISTANCE_PCT).
                # Three numbers for one limit. The DECLARED floor is left
                # alone here -- changing a risk limit is the owner's call --
                # and the question is recorded in the vault.
                ceiling = held
        if number > ceiling:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{section}.{rm_key}={number} is looser than the limit of "
                    f"{ceiling}. A client may tighten a risk limit but never "
                    f"loosen it, and the server enforces {ceiling} on every "
                    f"trade."
                ),
            )


@config_router.put(
    "/config",
    response_model=schemas.ApiResponseData[schemas.AppConfig],
    summary="Update configuration",
)
async def update_config_endpoint(
    new_config: schemas.AppConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) updating application configuration."
    )

    updated_any_section = False

    # The account's CURRENT risk settings. A client may tighten a limit and
    # never loosen one, so the ceiling for each limit is the value the
    # account already holds, or the server floor where the account holds
    # none.
    db_config = await crud.get_config_model(db, user_id=current_user.id)
    live_current: Dict[str, Any] = {}
    backtest_current: Dict[str, Any] = {}
    if db_config is not None:
        raw_live = getattr(db_config, "risk_management", None)
        if isinstance(raw_live, dict):
            live_current = dict(raw_live)
        raw_backtest = getattr(db_config, "backtest_risk_management", None)
        if isinstance(raw_backtest, dict):
            backtest_current = dict(raw_backtest)

    if new_config.risk_management:
        # ONLY the fields the client actually sent. Pydantic injects the
        # schema default for every field the client omitted, and an injected
        # default is neither a value the client asked for nor a value that
        # may be written over what the account already has.
        risk_payload = _the_fields_the_client_actually_sent(new_config.risk_management)
        # The per-trade risk is bounded by the server's floor: the page may
        # not display a limit the server will not honour.
        _refuse_a_risk_per_trade_above_the_floor(risk_payload, "risk_management")
        # And every account-level limit is bounded by the account's own
        # current value, or by the server floor where the account holds none:
        # a client may tighten a limit and never loosen one.
        _refuse_a_risk_limit_looser_than_its_ceiling(
            risk_payload, "risk_management", live_current
        )
        await crud.update_config_section(
            db,
            current_user.id,
            "risk_management",
            risk_payload,
        )
        updated_any_section = True
    if new_config.exchange_settings:
        await crud.update_config_section(
            db,
            current_user.id,
            "exchange_settings",
            new_config.exchange_settings.model_dump(by_alias=True),
        )
        updated_any_section = True
    if new_config.notifications:
        await crud.update_config_section(
            db,
            current_user.id,
            "notifications",
            new_config.notifications.model_dump(by_alias=True),
        )
        updated_any_section = True
    if new_config.data_sources:
        await crud.update_config_section(
            db, current_user.id, "data_sources", new_config.data_sources
        )
        updated_any_section = True
    if new_config.backtest_risk_management:
        # Only the fields the client actually sent -- same reason as the live
        # section above.
        backtest_risk_payload = _the_fields_the_client_actually_sent(
            new_config.backtest_risk_management
        )
        # The paper account is the one the bot sizes its paper trades with
        # (bot_module/risk_manager.py:188-195), so it is bounded the same way.
        _refuse_a_risk_per_trade_above_the_floor(
            backtest_risk_payload, "backtest_risk_management"
        )
        _refuse_a_risk_limit_looser_than_its_ceiling(
            backtest_risk_payload, "backtest_risk_management", backtest_current
        )
        await crud.update_config_section(
            db,
            current_user.id,
            "backtest_risk_management",
            backtest_risk_payload,
        )
        updated_any_section = True

    if not updated_any_section:
        logger.info(
            f"User '{current_user.username}' (ID: {current_user.id}) - No specific configuration sections provided for update."
        )
        pass

    await db.commit()

    updated_db_config = await crud.get_config(db, user_id=current_user.id)
    if not updated_db_config:
        logger.error(
            f"User '{current_user.username}' (ID: {current_user.id}) - Failed to retrieve configuration after update."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve configuration after update.",
        )

    # --- Publish RELOAD_CONFIG command to apply settings instantly in the bot ---
    if updated_any_section:
        try:
            reload_command = {
                "command": "RELOAD_CONFIG",
                "payload": {"user_id": current_user.id},
            }
            await redis_client.publish(
                REDIS_COMMAND_CHANNEL, json.dumps(reload_command)
            )
            logger.info(
                f"User '{current_user.username}' (ID: {current_user.id}) - RELOAD_CONFIG command published to bot."
            )
        except Exception as e:
            # Do not block response on publish error - settings will apply on next reload cycle anyway
            logger.warning(
                f"Failed to publish RELOAD_CONFIG command for user {current_user.id}: {e}"
            )

    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) - Application configuration updated successfully."
    )
    return {"data": updated_db_config}


@config_router.post(
    "/config/datasources/symbols", response_model=schemas.ApiResponseData
)
async def add_symbol(
    payload: schemas.SymbolPayload,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) attempting to add symbol: {payload.symbol}"
    )
    updated_sources = await crud.add_symbol_to_config(
        db, user_id=current_user.id, symbol=payload.symbol
    )
    if updated_sources is None:
        logger.error(
            f"User '{current_user.username}' (ID: {current_user.id}) - Failed to add symbol {payload.symbol}. User configuration (AppConfig) not found."
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User configuration not found. Cannot add symbol.",
        )

    await db.commit()
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) successfully added symbol: {payload.symbol}. Current symbols: {updated_sources.get('symbols')}"
    )
    return {"data": updated_sources}


@config_router.delete(
    "/config/datasources/symbols/{symbol}", response_model=schemas.ApiResponseData
)
async def delete_symbol(
    symbol: str,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) attempting to delete symbol: {symbol}"
    )
    updated_sources = await crud.delete_symbol_from_config(
        db, user_id=current_user.id, symbol=symbol
    )
    if updated_sources is None:
        logger.error(
            f"User '{current_user.username}' (ID: {current_user.id}) - Failed to delete symbol {symbol}. User configuration (AppConfig) not found or symbol already not present."
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User configuration not found or symbol not in list.",
        )

    await db.commit()
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) successfully deleted symbol: {symbol}. Current symbols: {updated_sources.get('symbols')}"
    )
    return {"data": updated_sources}


@config_router.get(
    "/config/blacklist",
    response_model=schemas.ApiResponseData[schemas.BlacklistSettings],
)
async def get_blacklist(
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Returns current coin blacklist of the user.
    Automatically clears expired entries before returning.
    """
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) requesting blacklist."
    )

    config = await crud.get_config(db, user_id=current_user.id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User configuration not found.",
        )

    # Get blacklist from risk_management
    risk_management = config.risk_management or {}
    # Convert Pydantic model to dictionary if needed
    if hasattr(risk_management, "model_dump"):
        risk_management = risk_management.model_dump(mode="json")
    elif not isinstance(risk_management, dict):
        risk_management = {}

    blacklist_data = risk_management.get("blacklist") or {"coins": []}

    # Clear expired entries
    now = datetime.now(timezone.utc)
    active_coins = []
    for coin in blacklist_data.get("coins", []):
        until_str = coin.get("until")
        if until_str:
            try:
                until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
                if until_dt > now:
                    active_coins.append(coin)
            except (ValueError, TypeError):
                # If date parsing fails, consider it permanent
                active_coins.append(coin)
        else:
            # until is None = permanent
            active_coins.append(coin)

    # If there were changes, save the cleared list
    if len(active_coins) != len(blacklist_data.get("coins", [])):
        blacklist_data["coins"] = active_coins
        risk_management["blacklist"] = blacklist_data
        await crud.update_config_section(
            db, current_user.id, "risk_management", risk_management
        )
        await db.commit()

    # Get autoRules
    auto_rules_data = blacklist_data.get("autoRules", [])
    auto_rules = (
        [schemas.AutoBlacklistRule(**rule) for rule in auto_rules_data]
        if auto_rules_data
        else []
    )

    return {
        "data": schemas.BlacklistSettings(
            coins=[schemas.BlacklistedCoin(**coin) for coin in active_coins],
            auto_rules=auto_rules,
        )
    }


@config_router.post(
    "/config/blacklist",
    response_model=schemas.ApiResponseData[schemas.BlacklistSettings],
)
async def add_to_blacklist(
    payload: schemas.AddToBlacklistPayload,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Adds a coin to the user's blacklist.
    """
    symbol = payload.symbol.upper().strip()
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) adding {symbol} to blacklist with duration: {payload.duration}"
    )

    config = await crud.get_config(db, user_id=current_user.id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User configuration not found.",
        )

    # Determine block time
    until: Optional[datetime] = None
    if payload.duration == "end_of_day":
        # End of current day UTC
        now = datetime.now(timezone.utc)
        until = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif payload.duration == "custom" and payload.custom_until:
        until = payload.custom_until
    # permanent -> until remains None

    # Get current blacklist
    risk_management = config.risk_management or {}
    # Convert Pydantic model to dictionary if needed
    if hasattr(risk_management, "model_dump"):
        risk_management = risk_management.model_dump(mode="json")
    elif isinstance(risk_management, str):
        import json

        risk_management = json.loads(risk_management)
    elif not isinstance(risk_management, dict):
        risk_management = {}

    blacklist_data = risk_management.get("blacklist", {"coins": []})
    if not isinstance(blacklist_data, dict):
        blacklist_data = {"coins": []}

    coins = blacklist_data.get("coins", [])
    if not isinstance(coins, list):
        coins = []

    # Check if such coin already exists
    for coin in coins:
        if coin.get("symbol", "").upper() == symbol:
            # Update existing entry
            coin["until"] = until.isoformat() if until else None
            coin["reason"] = payload.reason
            coin["addedAt"] = datetime.now(timezone.utc).isoformat()
            logger.info(f"Updated existing blacklist entry for {symbol}")
            break
    else:
        # Add new entry
        new_coin = {
            "symbol": symbol,
            "until": until.isoformat() if until else None,
            "reason": payload.reason,
            "addedAt": datetime.now(timezone.utc).isoformat(),
        }
        coins.append(new_coin)
        logger.info(f"Added new blacklist entry for {symbol}")

    # Save updated blacklist
    blacklist_data["coins"] = coins
    risk_management["blacklist"] = blacklist_data
    await crud.update_config_section(
        db, current_user.id, "risk_management", risk_management
    )
    await db.commit()

    try:
        redis = await get_redis_client()

        # 1. Update simple blacklist set (hft:blacklist:{user_id}) for fast lookup
        # Only active, existing coins
        active_symbols = [c.get("symbol", "").upper() for c in coins if c.get("symbol")]
        await redis.set(f"hft:blacklist:{current_user.id}", json.dumps(active_symbols))

        # 2. Publish full UpdateBlacklist command to HFT engine
        # Need to construct the full settings object
        auto_rules = blacklist_data.get("autoRules", [])
        settings = {"coins": coins, "autoRules": auto_rules}

        cmd = {
            "action": "UpdateBlacklist",
            "user_id": current_user.id,
            "settings": settings,
        }
        await redis.publish(HFT_CMD_CHANNEL, json.dumps(cmd))
        logger.info(f"Published UpdateBlacklist command for user {current_user.id}")

    except Exception as e:
        logger.error(f"Failed to sync blacklist to Redis: {e}")

    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) successfully added {symbol} to blacklist."
    )

    return {
        "data": schemas.BlacklistSettings(
            coins=[schemas.BlacklistedCoin(**coin) for coin in coins]
        )
    }


@config_router.delete(
    "/config/blacklist/{symbol}",
    response_model=schemas.ApiResponseData[schemas.BlacklistSettings],
)
async def remove_from_blacklist(
    symbol: str,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Removes a coin from the user's blacklist.
    """
    symbol = symbol.upper().strip()
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) removing {symbol} from blacklist."
    )

    config = await crud.get_config(db, user_id=current_user.id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User configuration not found.",
        )

    # Get current blacklist
    risk_management = config.risk_management or {}
    # Convert Pydantic model to dictionary if needed
    if hasattr(risk_management, "model_dump"):
        risk_management = risk_management.model_dump(mode="json")
    elif isinstance(risk_management, str):
        import json

        risk_management = json.loads(risk_management)
    elif not isinstance(risk_management, dict):
        risk_management = {}

    blacklist_data = risk_management.get("blacklist", {"coins": []})
    if not isinstance(blacklist_data, dict):
        blacklist_data = {"coins": []}

    coins = blacklist_data.get("coins", [])
    if not isinstance(coins, list):
        coins = []

    # Filter coin
    original_count = len(coins)
    coins = [coin for coin in coins if coin.get("symbol", "").upper() != symbol]

    if len(coins) == original_count:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Symbol {symbol} not found in blacklist.",
        )

    # Save updated blacklist
    blacklist_data["coins"] = coins
    risk_management["blacklist"] = blacklist_data
    await crud.update_config_section(
        db, current_user.id, "risk_management", risk_management
    )
    await db.commit()

    try:
        redis = await get_redis_client()

        active_symbols = [c.get("symbol", "").upper() for c in coins if c.get("symbol")]
        await redis.set(f"hft:blacklist:{current_user.id}", json.dumps(active_symbols))

        auto_rules = blacklist_data.get("autoRules", [])
        settings = {"coins": coins, "autoRules": auto_rules}

        cmd = {
            "action": "UpdateBlacklist",
            "user_id": current_user.id,
            "settings": settings,
        }
        await redis.publish(HFT_CMD_CHANNEL, json.dumps(cmd))
        logger.info(f"Published UpdateBlacklist command for user {current_user.id}")

    except Exception as e:
        logger.error(f"Failed to sync blacklist to Redis: {e}")

    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) successfully removed {symbol} from blacklist."
    )

    return {
        "data": schemas.BlacklistSettings(
            coins=[schemas.BlacklistedCoin(**coin) for coin in coins]
        )
    }


@config_router.put(
    "/config/blacklist/rules",
    response_model=schemas.ApiResponseData[schemas.BlacklistSettings],
)
async def update_auto_blacklist_rules(
    payload: schemas.UpdateAutoBlacklistRulesPayload,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Updates automatic block rules for the user.
    """
    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) updating auto-blacklist rules. Count: {len(payload.autoRules)}"
    )

    config = await crud.get_config(db, user_id=current_user.id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User configuration not found.",
        )

    # Get current risk_management
    risk_management = config.risk_management or {}
    if hasattr(risk_management, "model_dump"):
        risk_management = risk_management.model_dump(mode="json")
    elif isinstance(risk_management, str):
        import json

        risk_management = json.loads(risk_management)
    elif not isinstance(risk_management, dict):
        risk_management = {}

    blacklist_data = risk_management.get("blacklist", {"coins": []})
    if not isinstance(blacklist_data, dict):
        blacklist_data = {"coins": []}

    # Update auto_rules, serializing rules to dictionaries with camelCase keys
    blacklist_data["autoRules"] = [
        rule.model_dump(mode="json", by_alias=True) for rule in payload.autoRules
    ]

    # Save updated blacklist
    risk_management["blacklist"] = blacklist_data
    await crud.update_config_section(
        db, current_user.id, "risk_management", risk_management
    )
    await db.commit()

    try:
        redis = await get_redis_client()

        coins = blacklist_data.get("coins", [])
        active_symbols = [c.get("symbol", "").upper() for c in coins if c.get("symbol")]
        await redis.set(f"hft:blacklist:{current_user.id}", json.dumps(active_symbols))

        # Payload autoRules are already Pydantic models in the input, but we serialized them for DB
        # Use the raw JSON list we just created
        settings = {"coins": coins, "autoRules": blacklist_data["autoRules"]}

        cmd = {
            "action": "UpdateBlacklist",
            "user_id": current_user.id,
            "settings": settings,
        }
        await redis.publish(HFT_CMD_CHANNEL, json.dumps(cmd))
        logger.info(f"Published UpdateBlacklist command for user {current_user.id}")

    except Exception as e:
        logger.error(f"Failed to sync blacklist to Redis: {e}")

    logger.info(
        f"User '{current_user.username}' (ID: {current_user.id}) successfully updated auto-blacklist rules."
    )

    return {
        "data": schemas.BlacklistSettings(
            coins=[
                schemas.BlacklistedCoin(**coin)
                for coin in blacklist_data.get("coins", [])
            ],
            auto_rules=payload.autoRules,
        )
    }


@config_router.get(
    "/config/block-restrictions",
    response_model=schemas.ApiResponseData[schemas.BlockRestrictionsConfig],
)
async def get_block_restrictions(current_user: models.User = Depends(get_current_user)):
    restrictions = plans_config.get_block_restrictions()
    return {"data": restrictions}
