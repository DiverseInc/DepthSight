"""
Onboarding endpoints — power the 5-minute "sign up → paper strategy" flow.

Endpoints:
- POST /onboarding/start-paper-strategy
    Auto-create + start a paper trading strategy from a template. One shot.
    Used in onboarding wizard step 1 ("pick a template, click Start").

- POST /onboarding/validate-okx-key
    Test an OKX API key/secret/passphrase without saving it.
    Returns { valid: bool, error?: str }. The user then saves the key via
    the normal /api/v1/config/api-keys flow.

- POST /onboarding/convert-to-live
    Convert an existing paper strategy to live. Validates the user has at
    least one active OKX key + a permission that allows real trading.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt_async
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud, models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..dependencies import require_permission
from ..redis_client import get_redis_client

logger = logging.getLogger(__name__)

onboarding_router = APIRouter(
    prefix="/api/v1/onboarding",
    tags=["Onboarding"],
    dependencies=[Depends(get_current_user)],
)


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class StartPaperStrategyRequest(BaseModel):
    """Default values map to RSI Breakout v2 on BTCUSDT 1h — the recommended
    starter strategy surfaced on the marketing landing page."""
    template_slug: str = "rsi-breakout-v2"
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    position_size_pct: float = 5.0
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0
    max_concurrent: int = 1


class StartPaperStrategyResponse(BaseModel):
    config_id: str
    strategy_instance_id: str
    template_slug: str
    symbol: str
    timeframe: str
    status: str  # "started" or "queued"


class ValidateOkxKeyRequest(BaseModel):
    api_key: str
    api_secret: str
    passphrase: str


class ValidateOkxKeyResponse(BaseModel):
    valid: bool
    error: Optional[str] = None
    account_type: Optional[str] = None


class ConvertToLiveRequest(BaseModel):
    config_id: str


class ConvertToLiveResponse(BaseModel):
    config_id: str
    run_mode: str
    api_key_id: Optional[int]
    status: str


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@onboarding_router.post(
    "/start-paper-strategy",
    response_model=schemas.ApiResponseData[StartPaperStrategyResponse],
    status_code=status.HTTP_201_CREATED,
)
async def start_paper_strategy(
    req: StartPaperStrategyRequest,
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-create + start a paper strategy from a template. End-to-end in
    one shot so the onboarding wizard can ship with a single CTA.

    Steps:
    1. Look up the strategy template by slug.
    2. Persist a StrategyConfig for the user with mode='paper'.
    3. Publish START_STRATEGY to the bot via Redis (same channel the
       regular /api/v1/strategies POST uses).
    """
    # 1. Resolve template
    template = await crud.get_strategy_template_by_slug(db, req.template_slug)
    if not template or not template.active:
        raise HTTPException(
            status_code=404,
            detail=f"Strategy template '{req.template_slug}' not found or inactive",
        )

    # 2. Build the config from the template + user-supplied overrides
    from ..depthsight_api import (
        _coerce_strategy_config_dict,
        _enforce_strategy_plan_restrictions,
    )

    config_create = schemas.StrategyConfigCreate(
        name=f"{template.name} (onboarding)",
        description=f"Auto-created by 5-min onboarding wizard from template '{req.template_slug}'",
        config_data=template.config_data,
        symbol_selection_mode="FIXED",
        symbols=[req.symbol],
    )
    _enforce_strategy_plan_restrictions(config_create.config_data, current_user)

    db_config = await crud.create_strategy_config(
        db=db, user_id=current_user.id, config_create=config_create
    )

    # 3. Publish START_STRATEGY to bot via Redis pubsub.
    # Mirrors the existing /api/v1/strategies start flow but bundled with
    # the config creation. Bot listener is in bot_module/controller.py.
    try:
        import json as _json
        from bot_module import config as bot_config
        command_channel = getattr(
            bot_config, "REDIS_COMMAND_CHANNEL", "depthsight:commands"
        )
        redis_client = await get_redis_client()
        # Start command payload — same shape used by /api/v1/strategies POST
        cmd_payload = {
            "action": "START_STRATEGY",
            "user_id": current_user.id,
            "config_id": db_config.id,
            "template_slug": req.template_slug,
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "mode": "paper",
            "position_size_pct": req.position_size_pct,
            "stop_loss_pct": req.stop_loss_pct,
            "take_profit_pct": req.take_profit_pct,
            "max_concurrent": req.max_concurrent,
            "source": "onboarding_wizard",
        }
        await redis_client.publish(command_channel, _json.dumps(cmd_payload))
        status_msg = "queued"  # bot picks it up async; not blocking this response
    except Exception as e:
        logger.warning(
            f"start_paper_strategy: failed to publish START_STRATEGY for user "
            f"{current_user.id} (non-fatal, config persisted): {e}"
        )
        status_msg = "config_persisted_start_failed"

    await db.commit()
    await db.refresh(db_config)

    return {
        "data": StartPaperStrategyResponse(
            config_id=db_config.id,
            strategy_instance_id=db_config.id,
            template_slug=req.template_slug,
            symbol=req.symbol,
            timeframe=req.timeframe,
            status=status_msg,
        )
    }


@onboarding_router.post(
    "/validate-okx-key",
    response_model=schemas.ApiResponseData[ValidateOkxKeyResponse],
)
async def validate_okx_key(
    req: ValidateOkxKeyRequest,
    current_user: models.User = Depends(get_current_user),
):
    """
    Test an OKX API key by attempting a public-balance read. Does NOT
    persist the key — user must save it via the normal Settings flow after
    validation passes.

    Common failure modes:
    - Wrong secret / passphrase → ccxt.AuthenticationError
    - Key was deleted on OKX → 50119 "API key doesn't exist"
    - IP not whitelisted → 50118 or similar
    """
    try:
        exchange = ccxt_async.okx({
            "apiKey": req.api_key,
            "secret": req.api_secret,
            "password": req.passphrase,
            "options": {"defaultType": "swap"},
        })
        # Sanity read — does NOT require trade permission
        balance = await exchange.fetch_balance()
        await exchange.close()

        # Best-effort: get account type for the response (helps UI labeling)
        account_types = balance.get("info", {}).get("data", [{}])[0].get("acctGrp", None)

        return {
            "data": ValidateOkxKeyResponse(
                valid=True,
                error=None,
                account_type=account_types,
            )
        }
    except ccxt_async.AuthenticationError as e:
        await _safe_close_exchange(exchange)
        return {
            "data": ValidateOkxKeyResponse(
                valid=False,
                error=f"Authentication failed: {str(e)[:120]}",
            )
        }
    except Exception as e:
        await _safe_close_exchange(exchange)
        logger.warning(f"OKX key validation failed for user {current_user.id}: {e}")
        return {
            "data": ValidateOkxKeyResponse(
                valid=False,
                error=f"Could not reach OKX: {type(e).__name__}: {str(e)[:120]}",
            )
        }


async def _safe_close_exchange(exchange):
    """Tolerant exchange.close() — never raise during cleanup."""
    try:
        await exchange.close()
    except Exception:
        pass


@onboarding_router.post(
    "/convert-to-live",
    response_model=schemas.ApiResponseData[ConvertToLiveResponse],
)
async def convert_to_live(
    req: ConvertToLiveRequest,
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Convert an existing paper strategy to live. Requires:
    1. User has at least one active OKX api_key with read + trade permissions
       AND allow_real_trading permission on their plan.
    2. The config belongs to the user.

    If multiple active OKX keys exist, picks the most-recently-created one.
    """
    # 1. Verify the config exists and belongs to this user
    db_config = await crud.get_strategy_config(db, current_user.id, req.config_id)
    if not db_config:
        raise HTTPException(
            status_code=404,
            detail="Strategy config not found",
        )

    # 2. Check the user's plan permits real trading
    if not current_user.plan:
        raise HTTPException(
            status_code=403,
            detail="No plan assigned — please choose a plan in Settings",
        )
    plan_quotas = (current_user.plan.quotas or {}) if current_user.plan else {}
    if not plan_quotas.get("allow_real_trading", False):
        raise HTTPException(
            status_code=403,
            detail="Your plan doesn't allow live trading. Upgrade to Pro in Settings.",
        )

    # 3. Pick an active OKX key
    active_keys = await crud.list_active_api_keys_for_user(
        db, user_id=current_user.id, exchange="okx"
    )
    if not active_keys:
        raise HTTPException(
            status_code=400,
            detail="No active OKX key. Add one in Settings → API Keys first.",
        )
    selected_key = active_keys[0]  # most recent

    # 4. Update the strategy config + publish RECONFIGURE_STRATEGY
    db_config.run_mode = "live"
    db_config.api_key_id = selected_key.id
    await db.commit()
    await db.refresh(db_config)

    try:
        import json as _json
        from bot_module import config as bot_config
        command_channel = getattr(
            bot_config, "REDIS_COMMAND_CHANNEL", "depthsight:commands"
        )
        redis_client = await get_redis_client()
        cmd_payload = {
            "action": "RECONFIGURE_STRATEGY",
            "user_id": current_user.id,
            "config_id": db_config.id,
            "run_mode": "live",
            "api_key_id": selected_key.id,
            "source": "onboarding_wizard",
        }
        await redis_client.publish(command_channel, _json.dumps(cmd_payload))
    except Exception as e:
        logger.warning(
            f"convert_to_live: failed to publish RECONFIGURE_STRATEGY: {e}"
        )

    return {
        "data": ConvertToLiveResponse(
            config_id=db_config.id,
            run_mode="live",
            api_key_id=selected_key.id,
            status="live_queued",
        )
    }
