"""Gate 4, Part F: the stop distance a NEW client is handed is 5%.

THE DECISION
============
On 2026-11-01 the owner decided that the maximum stop distance is 5%
EVERYWHERE. Three numbers describe one limit, and all three have to agree:

1. the DECLARED server floor -- ``bot_module/strategy_registry.py``
   ``SERVER_MAX_STOP_DISTANCE_PCT`` (5.0). This is the number the Strategy
   screen is handed in the client contract, and the ceiling the write
   guard applies to a limit an account holds none of.
2. the value a NEW account is HANDED -- ``api/schemas.py``
   ``RiskManagementSettings.maxStopDistancePct`` and
   ``BacktestRiskManagementSettings.maxStopDistancePct``. This was 10.0
   and is now 5.0.
3. the limit the server enforces for an account that holds no value at
   all -- the RiskManager's own fallback (5.0).

WHAT "EVERYWHERE" DOES NOT MEAN
================================
It does not mean "sweep every existing account". An account created
BEFORE the change holds 10.0 and keeps exactly 10.0: its stored block is
not rewritten by this change, and the RiskManager still enforces 10.0 for
it. The owner was told exactly this when the decision was made. What
changes is the value a NEW client is handed.

EVERY ACCOUNT HERE IS BUILT BY THE PRODUCT'S OWN CREATION PATH
===============================================================
``api/crud.py`` ``create_user`` / ``create_oauth_user`` /
``create_default_config_for_existing_user``. NOT a hand-made dict.

WHY THAT MATTERS: a probe that invents its own input can invent a defect.
An earlier probe in this repo built a ``risk_management`` block BY HAND
with five keys and no ``maxStopDistancePct``, and the server refused the
page's own 10.0 with a 422 -- which looked like a product defect and was
an artefact of the synthetic block. Every account in this file is created
the way a real client is created, so its stored block IS the block a real
client holds.

EVERY PAYLOAD HERE IS COMPLETE
==============================
On purpose. ``RiskManagementSettings`` has REQUIRED fields
(``maxDrawdown``, ``maxConcurrentTrades``, ``stopLossEnabled``), so a
partial body is rejected with a 422 "field required" that never reaches
the limit check. A test that passes without exercising the fix proves
nothing.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from api import crud, models, schemas
from bot_module import config as bot_config
from bot_module.exchanges import ExchangeExecutor
from bot_module.risk_manager import RiskManager
from bot_module.strategy import OrderMode, SignalDirection, StrategySignal
from bot_module.strategy_registry import SERVER_MAX_STOP_DISTANCE_PCT

CONFIG_URL = "/api/v1/config"

#: The number the owner decided on (2026-11-01): 5% everywhere.
EXPECTED_STOP_DISTANCE_PCT = 5.0

#: What the schema used to hand out. An account created before the change
#: still holds exactly this, and this file proves it is left alone.
OLD_DEFAULT_STOP_DISTANCE_PCT = 10.0


# --- helpers ----------------------------------------------------------------


async def _stored_block(
    db_session, user_id: int, section: str = "risk_management"
) -> Dict[str, Any]:
    """The block the DATABASE holds -- not the one the API serves."""
    db_session.expire_all()
    result = await db_session.execute(
        select(models.AppConfig).where(models.AppConfig.user_id == user_id)
    )
    config_row = result.scalars().first()
    assert config_row is not None, "the account has no AppConfig row"
    return dict(getattr(config_row, section) or {})


async def _set_stored_block(
    db_session, user_id: int, values: Dict[str, Any], section: str = "risk_management"
) -> None:
    """Write values into the account's stored block, the way an older
    account holds them.

    This is how an account that was created BEFORE the change is simulated:
    the value it holds is the value the old default wrote, and the change
    under test must not touch it.
    """
    db_session.expire_all()
    result = await db_session.execute(
        select(models.AppConfig).where(models.AppConfig.user_id == user_id)
    )
    config_row = result.scalars().first()
    assert config_row is not None, "the account has no AppConfig row"
    merged = dict(getattr(config_row, section) or {})
    merged.update(values)
    setattr(config_row, section, merged)
    await db_session.commit()


async def _new_account_via_create_user(db_session) -> models.User:
    """A NEW account, built by the product's own registration path."""
    unique_id = uuid.uuid4().hex[:6]
    user = await crud.create_user(
        db_session,
        schemas.UserCreate(
            username=f"stopdist_{unique_id}",
            email=f"stopdist_{unique_id}@example.com",
            password="testpassword123",
        ),
    )
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _a_complete_risk_block(**overrides: Any) -> Dict[str, Any]:
    """A risk block that the API accepts, with ONE field changed.

    The three fields below are REQUIRED by ``RiskManagementSettings``, so
    every body here is complete: the ONLY reason a request can be refused
    is the field under test.
    """
    block: Dict[str, Any] = {
        "maxDrawdown": 10.0,
        "maxConcurrentTrades": 5,
        "stopLossEnabled": True,
    }
    block.update(overrides)
    return block


def _signal_with_stop_distance(stop_loss: float) -> StrategySignal:
    """A signal whose stop sits a known distance below the entry.

    Entry is 50000, so a stop at 47000 is 6% away -- wider than a 5%
    limit, inside a 10% one.

    The take profit is 54000, which is FURTHER away than the stop. That
    matters: the risk/reward gate refuses a ratio below
    ``RISK_MANAGER_MIN_RR_RATIO`` (1.0) and it is checked AFTER the stop
    distance gate, so a take profit that is closer than the stop would
    have the signal refused for a reason that is not the one under test.
    With 4000 of upside against 3000 of downside the ratio is 1.33.
    """
    return StrategySignal(
        strategy_name="TestStrat",
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        trigger_price=50000.0,
        stop_loss=stop_loss,
        take_profit=54000.0,
        mode=OrderMode.MARKET,
    )


async def _risk_manager_for(
    db_session, user_id: int, stored_block: Dict[str, Any]
) -> RiskManager:
    """A RiskManager built from the ACCOUNT'S OWN stored risk block.

    The block is read out of the database and belongs to the account under
    test, so what the manager enforces is what that account really holds --
    not a value this file invented, and not another account's block.
    """
    executor = AsyncMock(spec=ExchangeExecutor)
    executor.get_account_balance.return_value = {
        "USDT": {"free": "10000.00", "locked": "0.00"}
    }
    manager = RiskManager(
        executor=executor,
        paper_executor=executor,
        user_id=user_id,
        db_session=db_session,
        user_settings={"risk_management": stored_block},
    )
    await manager.initialize()
    return manager


# --- the declared floor is already 5.0 --------------------------------------


def test_the_declared_server_floor_is_five_percent():
    """Number 1 of the three: the floor the platform DECLARES.

    ``bot_module/strategy_registry.py`` is the contract the Strategy screen
    is handed, so this is the number the client is shown as the cap.
    """
    assert SERVER_MAX_STOP_DISTANCE_PCT == EXPECTED_STOP_DISTANCE_PCT


# --- (b) and (c): the schema defaults ---------------------------------------


def test_the_live_risk_schema_default_is_five_percent():
    """(b) ``RiskManagementSettings`` defaults to 5%.

    The model has REQUIRED fields, so it cannot be constructed with no
    arguments at all. They are supplied here exactly as ``api/crud.py``
    supplies them, and ``maxStopDistancePct`` is NOT supplied -- so the
    value asserted here is the schema's DEFAULT and not a value anybody
    passed in.
    """
    field = schemas.RiskManagementSettings.model_fields["maxStopDistancePct"]
    assert field.default == EXPECTED_STOP_DISTANCE_PCT

    built = schemas.RiskManagementSettings(
        maxDrawdown=10.0,
        maxConsecutiveLosses=10,
        maxConcurrentTrades=5,
        stopLossEnabled=True,
        defaultStopLossPercent=2.0,
        riskPerTradePercent=1.0,
    )
    assert built.maxStopDistancePct == EXPECTED_STOP_DISTANCE_PCT


def test_the_backtest_risk_schema_default_is_five_percent():
    """(c) ``BacktestRiskManagementSettings`` defaults to 5%.

    Same reasoning as the live block: the required fields are supplied and
    the stop distance is not, so this is the DEFAULT.
    """
    field = schemas.BacktestRiskManagementSettings.model_fields["maxStopDistancePct"]
    assert field.default == EXPECTED_STOP_DISTANCE_PCT

    built = schemas.BacktestRiskManagementSettings(
        maxDrawdown=10.0,
        dailyMaxLossPercent=5.0,
        maxConsecutiveLosses=10,
        maxConcurrentTrades=5,
        stopLossEnabled=True,
        defaultStopLossPercent=2.0,
    )
    assert built.maxStopDistancePct == EXPECTED_STOP_DISTANCE_PCT


# --- (a) every account a NEW client can get --------------------------------


@pytest.mark.asyncio
async def test_a_new_account_is_handed_five_percent(db_session):
    """(a) The stored block of a NEW account holds 5%.

    Built by ``crud.create_user`` -- the product's own registration path.
    """
    user = await _new_account_via_create_user(db_session)

    stored = await _stored_block(db_session, user.id)
    assert stored["maxStopDistancePct"] == EXPECTED_STOP_DISTANCE_PCT


@pytest.mark.asyncio
async def test_a_new_oauth_account_is_handed_five_percent(db_session):
    """(a) The OAuth sign-up path hands out 5% as well.

    ``create_oauth_user`` builds its own default block; it is a second
    creation path, not a copy of the first one.
    """
    unique_id = uuid.uuid4().hex[:6]
    user = await crud.create_oauth_user(
        db_session,
        email=f"oauth_stopdist_{unique_id}@example.com",
        username=f"oauth_stopdist_{unique_id}",
    )
    await db_session.commit()

    stored = await _stored_block(db_session, user.id)
    assert stored["maxStopDistancePct"] == EXPECTED_STOP_DISTANCE_PCT


@pytest.mark.asyncio
async def test_a_legacy_account_without_a_config_is_handed_five_percent(db_session):
    """(a) The third creation path hands out 5% as well.

    ``create_default_config_for_existing_user`` builds the block for an
    account that has no configuration row yet.
    """
    user = await _new_account_via_create_user(db_session)

    # Take the account back to the state it was in before it had a config
    # row at all, then let the product build one.
    await db_session.execute(
        delete(models.AppConfig).where(models.AppConfig.user_id == user.id)
    )
    await db_session.commit()

    await crud.create_default_config_for_existing_user(db_session, user.id)
    await db_session.commit()

    stored = await _stored_block(db_session, user.id)
    assert stored["maxStopDistancePct"] == EXPECTED_STOP_DISTANCE_PCT


@pytest.mark.asyncio
async def test_the_backtest_block_of_a_new_account_is_handed_five_percent(db_session):
    """(a) The PAPER block of a NEW account holds 5% too.

    The paper account is the one the bot sizes its paper trades with, so
    it is a limit of its own and not a copy of the live one.
    """
    user = await _new_account_via_create_user(db_session)

    stored = await _stored_block(db_session, user.id, "backtest_risk_management")
    assert stored["maxStopDistancePct"] == EXPECTED_STOP_DISTANCE_PCT


@pytest.mark.asyncio
async def test_the_api_hands_a_new_client_five_percent(authenticated_client):
    """(a) The value a NEW client is actually SERVED is 5.0.

    The stored block is what the database holds; this is what the client
    is given, which is the number the owner was shown.
    """
    response = await authenticated_client.get(CONFIG_URL)
    assert response.status_code == 200, response.text

    served = response.json()["data"]["riskManagement"]
    assert served["maxStopDistancePct"] == EXPECTED_STOP_DISTANCE_PCT


# --- (d) an EXISTING account is left exactly as it is -----------------------


@pytest.mark.asyncio
async def test_an_existing_account_is_not_swept_by_the_change(
    authenticated_client, db_session, pro_user
):
    """(d) An account that already holds 10.0 keeps 10.0.

    The save below is the Risk Settings page's own save, and it does not
    mention the stop distance. An account created before the change must
    keep the limit it has today -- "everywhere" means "everywhere a NEW
    client is handed a value", not "sweep every existing account".
    """
    await _set_stored_block(
        db_session, pro_user.id, {"maxStopDistancePct": OLD_DEFAULT_STOP_DISTANCE_PCT}
    )

    response = await authenticated_client.put(
        CONFIG_URL, json={"riskManagement": _a_complete_risk_block()}
    )

    assert response.status_code == 200, response.text
    stored = await _stored_block(db_session, pro_user.id)
    assert stored["maxStopDistancePct"] == OLD_DEFAULT_STOP_DISTANCE_PCT, (
        "the change rewrote an existing account's stop distance"
    )


@pytest.mark.asyncio
async def test_an_existing_account_may_still_save_its_own_ten_percent(
    authenticated_client, db_session, pro_user
):
    """(d) The account's own value is still the ceiling for that account.

    ``api/routes/config.py`` lets the account's OWN value win where it is
    looser than the declared floor, because that value is what the
    RiskManager enforces for it. A save that carries 10.0 therefore
    succeeds and stores 10.0 -- the account is not silently tightened to
    5.0 behind its back.
    """
    await _set_stored_block(
        db_session, pro_user.id, {"maxStopDistancePct": OLD_DEFAULT_STOP_DISTANCE_PCT}
    )

    response = await authenticated_client.put(
        CONFIG_URL,
        json={
            "riskManagement": _a_complete_risk_block(
                maxStopDistancePct=OLD_DEFAULT_STOP_DISTANCE_PCT
            )
        },
    )

    assert response.status_code == 200, response.text
    stored = await _stored_block(db_session, pro_user.id)
    assert stored["maxStopDistancePct"] == OLD_DEFAULT_STOP_DISTANCE_PCT


@pytest.mark.asyncio
async def test_the_server_still_enforces_ten_percent_for_an_existing_account(
    db_session, pro_user, monkeypatch
):
    """(d) The server ENFORCES 10.0 for the account that holds 10.0.

    Driven through the REAL ``assess_signal`` path
    (``bot_module/risk_manager.py``), not a copy of its arithmetic: the
    signal's stop sits 6% below the entry, which a 5% limit refuses and
    a 10% limit allows.
    """
    # The notional cap is a separate limit and is not what is measured.
    monkeypatch.setattr(
        bot_config, "MAX_REAL_POSITION_SIZE_PCT_BALANCE", 100.0, raising=True
    )

    await _set_stored_block(
        db_session, pro_user.id, {"maxStopDistancePct": OLD_DEFAULT_STOP_DISTANCE_PCT}
    )
    stored = await _stored_block(db_session, pro_user.id)
    assert stored["maxStopDistancePct"] == OLD_DEFAULT_STOP_DISTANCE_PCT

    manager = await _risk_manager_for(db_session, pro_user.id, stored)

    approved, _quantity, _planned_risk, reason = await manager.assess_signal(
        _signal_with_stop_distance(47000.0),
        {"minQty": 0.0001, "maxQty": 9000.0, "stepSize": 0.00001},
        10.0,
    )

    assert reason != "SL_TOO_FAR", (
        "a 6% stop was refused for an account whose limit is 10%: "
        f"reason={reason!r}"
    )
    assert approved is True, f"the account's own 10% limit was not honoured: {reason!r}"


@pytest.mark.asyncio
async def test_the_server_enforces_five_percent_for_a_new_account(
    db_session, pro_user, monkeypatch
):
    """The same gate refuses the same 6% stop for a 5% account.

    The two tests above are one measurement seen from both sides: the
    stored number decides, and the stored number is 5.0 for a NEW account
    and 10.0 for an account that already had one.
    """
    monkeypatch.setattr(
        bot_config, "MAX_REAL_POSITION_SIZE_PCT_BALANCE", 100.0, raising=True
    )

    stored = await _stored_block(db_session, pro_user.id)
    assert stored["maxStopDistancePct"] == EXPECTED_STOP_DISTANCE_PCT

    manager = await _risk_manager_for(db_session, pro_user.id, stored)

    approved, _quantity, _planned_risk, reason = await manager.assess_signal(
        _signal_with_stop_distance(47000.0),
        {"minQty": 0.0001, "maxQty": 9000.0, "stepSize": 0.00001},
        10.0,
    )

    assert approved is False
    assert reason == "SL_TOO_FAR", (
        f"a 6% stop was not refused for a 5% account: reason={reason!r}"
    )
