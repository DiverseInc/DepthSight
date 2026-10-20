"""`GET /api/v1/portfolio` must report the account's REAL trading gate.

WHY THIS TEST EXISTS
--------------------
`is_trading_allowed` is the platform's only "is this account allowed to trade"
indicator, and the dashboard renders it. It could never report `False`.

`TradingController._publish_state_to_redis()` publishes the genuine flag:

    "is_trading_allowed": getattr(self.rm, "_is_trading_allowed", True)

but the API's multi-controller aggregation branch summed ONLY the money fields
into a fresh dict that never contained that key. The mapping below it then read

    is_trading_allowed=aggregated_status.get("is_trading_allowed", True)

which therefore always hit the `True` default. The LIVE branch has the same
problem, hardcoding `is_trading_allowed=True` outright.

The consequence is precisely the state observed on 2026-10-18: the dashboard
showed strategies "RUNNING" while every signal was being rejected by
`rm.is_symbol_trading_allowed`, and the portfolio read `is_trading_allowed:
true`. The one signal that should have said "trading is switched off" was
structurally incapable of saying it.

These drive the real endpoint through the app's fixtures.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import MagicMock
from datetime import datetime, timezone

from api import models
from bot_module.config import REDIS_STATE_KEY_PORTFOLIO

pytestmark = pytest.mark.asyncio


async def _force_redis_fallback(db_session: AsyncSession, user) -> None:
    """Remove API keys so the endpoint cannot reach the exchange and must read Redis."""
    await db_session.execute(delete(models.ApiKey).where(models.ApiKey.user_id == user.id))
    await db_session.commit()


def _state(equity: float, allowed: bool) -> dict:
    return {
        "total_wallet_balance": equity,
        "total_equity": equity,
        "total_unrealized_pnl": 0.0,
        "today_pnl": 0.0,
        "is_trading_allowed": allowed,
        "consecutive_losses": 0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


async def test_a_blocked_account_is_reported_as_blocked(
    authenticated_client: AsyncClient,
    override_redis_client: MagicMock,
    pro_user: models.User,
    db_session: AsyncSession,
):
    """THE PROOF: a controller that has switched trading off must be visible.

    On the unfixed code this returns `true` regardless of what the bot
    published, which is how a fully-blocked account looked healthy.

    The key is a LIVE one (numeric api_key_id suffix). This request is in LIVE
    mode, and the Redis fallback now aggregates the LIVE controllers only: a
    PAPER controller's state (suffix "None") is a different account's, and
    bot_runner.py starts a paper controller for every user who does not have
    one -- so a ":None" key sits next to the live keys for most users. Reading
    it here made `?mode=live` report the paper account's balance and gate as
    the live ones. A live request with no live controller now takes the
    existing 404 path instead (see
    tests/test_portfolio_reports_risk_state.py), and the PAPER-mode
    equivalent is covered by
    tests/test_paper_portfolio_reports_unrealized_pnl.py.
    """
    await _force_redis_fallback(db_session, pro_user)
    await override_redis_client.set_initial_data(
        f"{REDIS_STATE_KEY_PORTFOLIO}:{pro_user.id}:1",
        _state(2645.55, allowed=False),
    )

    response = await authenticated_client.get("/api/v1/portfolio")

    assert response.status_code == 200
    assert response.json()["data"]["is_trading_allowed"] is False, (
        "the bot published is_trading_allowed=False for this controller but the "
        "API reported True -- the safety indicator cannot fail, so a blocked "
        "account is indistinguishable from a healthy one"
    )


async def test_one_blocked_controller_blocks_the_whole_account(
    authenticated_client: AsyncClient,
    override_redis_client: MagicMock,
    pro_user: models.User,
    db_session: AsyncSession,
):
    """Two controllers, one blocked: the account is not fully tradable."""
    await _force_redis_fallback(db_session, pro_user)
    await override_redis_client.set_initial_data(
        f"{REDIS_STATE_KEY_PORTFOLIO}:{pro_user.id}:None",
        _state(2645.55, allowed=True),
    )
    await override_redis_client.set_initial_data(
        f"{REDIS_STATE_KEY_PORTFOLIO}:{pro_user.id}:1",
        _state(1000.0, allowed=False),
    )

    response = await authenticated_client.get("/api/v1/portfolio")

    assert response.status_code == 200
    assert response.json()["data"]["is_trading_allowed"] is False


async def test_all_healthy_controllers_report_allowed(
    authenticated_client: AsyncClient,
    override_redis_client: MagicMock,
    pro_user: models.User,
    db_session: AsyncSession,
):
    """Positive control: the fix is not 'always report blocked'.

    Without this, a change that hardcoded `False` would pass the two tests
    above while reporting a permanent outage.
    """
    await _force_redis_fallback(db_session, pro_user)
    await override_redis_client.set_initial_data(
        f"{REDIS_STATE_KEY_PORTFOLIO}:{pro_user.id}:None",
        _state(2645.55, allowed=True),
    )
    await override_redis_client.set_initial_data(
        f"{REDIS_STATE_KEY_PORTFOLIO}:{pro_user.id}:1",
        _state(1000.0, allowed=True),
    )

    response = await authenticated_client.get("/api/v1/portfolio")

    assert response.status_code == 200
    assert response.json()["data"]["is_trading_allowed"] is True