"""Gate 4, Part C: the registry must be the authority AT THE REQUEST BOUNDARY.

THE DEFECT
==========
``bot_module/strategy_registry.py`` was a complete, correct contract -- the
validator, the server floor, the account-aware clamp -- and it was imported by
NOTHING in ``api/``. No route, schema or controller called it. Every guarantee
it encodes existed only inside the two test files, which exercised it in
isolation. That is exactly why CI was green while the product enforced
nothing:

* a raw ``POST /strategies/config`` with ``daily_loss_limit_pct = 99``, an
  unknown risk key, or ``strategy_name = "NoSuchStrategy"`` was STORED, not
  refused;
* the client's risk limits landed in ``config_data["risk_settings"]``, which
  was not a declared field of ``StrategyV2ConfigData``, so ``model_dump()``
  dropped them before the row was written;
* and nothing ever merged the client's limits into the account's
  ``risk_management`` block -- which is the ONLY place the bot reads them
  from (``bot_module/risk_manager.py:179``).

WHAT THIS TEST PROVES
=====================
* a payload the contract refuses is refused with 422 and NOTHING is written;
* the client's risk limits are MERGED into the account's ``risk_management``
  block -- merged, not replaced, and never looser than the account's own
  value;
* the contract (the strategy list and the server floor) is served over HTTP,
  so the client stops carrying its own hand-written copy of it;
* the REAL editor payload keeps its ``risk_settings`` and ``strategy_params``
  through the API schema.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from sqlalchemy import select

from api import models, schemas
from bot_module.strategy_registry import (
    CLIENT_SELECTABLE_STRATEGIES,
    SERVER_RISK_FLOOR,
)

STRATEGIES_URL = "/api/v1/strategies/config"
REGISTRY_URL = "/api/v1/strategies/registry"


# --- the payload the editor really sends --------------------------------------
#
# This is the shape ``frontend/src/stores/strategyEditorStore.ts``
# (``getDefaultInitialState`` + ``toJson``) produces. It MATTERS that this
# payload satisfies ``StrategyV2ConfigData``: the schema field is a union, so
# a payload that does NOT satisfy the model falls through to the plain-dict
# branch, and a test built on such a payload would never exercise the model
# at all -- it would pass while the real payload lost its risk settings.


def _real_editor_payload(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "strategy_name": "VolumeBreakout",
        "signal_source": "internal",
        "symbol": "BTCUSDT",
        "marketType": "FUTURES",
        "min_foundation_weight_threshold": 0,
        "foundation_weights": None,
        "filters": {"id": "filters_root", "type": "AND", "children": []},
        "entryTrigger": {"type": "on_candle_close", "timeframe": "5m"},
        "entryConditions": {"id": "entry_root", "type": "AND", "children": []},
        "initialization": {
            "id": "init_action_1",
            "type": "open_position",
            "params": {"direction": "LONG"},
        },
        "positionManagement": [],
        "oracle_regime": None,
        "oracle_confidence": 0,
        "use_ml_confirmation": False,
        "breakeven_on_regime_change": False,
    }
    payload.update(overrides)
    return payload


def _save_payload(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "name": "Gate 4 contract",
        "description": "written by the strategy editor",
        "config_data": _real_editor_payload(**overrides),
        "symbol_selection_mode": "STATIC",
        "symbols": ["BTCUSDT"],
        "use_ml_confirmation": False,
        "foundation_weights": None,
    }
    return body


async def _stored_configs(db_session, user_id: int) -> list:
    # The API writes through its own session; this one must not serve a
    # cached copy of the row.
    db_session.expire_all()
    result = await db_session.execute(
        select(models.StrategyConfig).where(models.StrategyConfig.user_id == user_id)
    )
    return list(result.scalars().all())


async def _account_risk_management(db_session, user_id: int) -> Dict[str, Any]:
    db_session.expire_all()
    result = await db_session.execute(
        select(models.AppConfig).where(models.AppConfig.user_id == user_id)
    )
    config = result.scalars().first()
    assert config is not None, "the account has no AppConfig row"
    return dict(config.risk_management or {})


async def _set_account_risk_management(
    db_session, user_id: int, values: Dict[str, Any]
) -> None:
    result = await db_session.execute(
        select(models.AppConfig).where(models.AppConfig.user_id == user_id)
    )
    config = result.scalars().first()
    assert config is not None, "the account has no AppConfig row"
    merged = dict(config.risk_management or {})
    merged.update(values)
    config.risk_management = merged
    await db_session.commit()


# --- item 1: a refused payload is refused, and nothing is written ------------


@pytest.mark.parametrize(
    "config_data_overrides,expected_text",
    [
        ({"risk_settings": {"daily_loss_limit_pct": 99}}, "daily_loss_limit_pct"),
        ({"risk_settings": {"unknown_field": 1}}, "unknown_field"),
        ({"strategy_name": "NoSuchStrategy"}, "NoSuchStrategy"),
    ],
)
@pytest.mark.asyncio
async def test_post_refuses_a_payload_the_contract_refuses(
    authenticated_client, db_session, pro_user, config_data_overrides, expected_text
):
    """The floor is enforced at the request boundary, and the row is NOT written."""
    response = await authenticated_client.post(
        STRATEGIES_URL, json=_save_payload(**config_data_overrides)
    )

    assert response.status_code == 422, response.text
    body = response.json()
    issues = body["error"]["issues"]
    assert any(expected_text in issue for issue in issues), issues

    assert await _stored_configs(db_session, pro_user.id) == []


@pytest.mark.parametrize(
    "config_data_overrides,expected_text",
    [
        ({"risk_settings": {"daily_loss_limit_pct": 99}}, "daily_loss_limit_pct"),
        ({"risk_settings": {"unknown_field": 1}}, "unknown_field"),
        ({"strategy_name": "NoSuchStrategy"}, "NoSuchStrategy"),
    ],
)
@pytest.mark.asyncio
async def test_put_refuses_a_payload_the_contract_refuses(
    authenticated_client, db_session, pro_user, config_data_overrides, expected_text
):
    """The same refusal on the update path, and the saved row is untouched."""
    created = await authenticated_client.post(STRATEGIES_URL, json=_save_payload())
    assert created.status_code == 201, created.text
    config_id = created.json()["data"]["id"]

    response = await authenticated_client.put(
        f"{STRATEGIES_URL}/{config_id}",
        json={"config_data": _real_editor_payload(**config_data_overrides)},
    )

    assert response.status_code == 422, response.text
    issues = response.json()["error"]["issues"]
    assert any(expected_text in issue for issue in issues), issues

    stored = await _stored_configs(db_session, pro_user.id)
    assert len(stored) == 1
    assert stored[0].config_data.get("risk_settings") is None


# --- item 2: the client's limits are merged into the account's ---------------


@pytest.mark.asyncio
async def test_a_tightened_limit_is_merged_into_the_account_risk_settings(
    authenticated_client, db_session, pro_user
):
    """The client's limit lands where the bot reads it, and the rest is kept."""
    before = await _account_risk_management(db_session, pro_user.id)
    untouched_key = "maxConcurrentTrades"
    assert untouched_key in before

    response = await authenticated_client.post(
        STRATEGIES_URL,
        json=_save_payload(risk_settings={"max_drawdown_pct": 3.0}),
    )
    assert response.status_code == 201, response.text

    after = await _account_risk_management(db_session, pro_user.id)
    assert after["maxDrawdown"] == 3.0
    # A field the client did not send keeps the account's value.
    assert after[untouched_key] == before[untouched_key]
    # And the merge did not replace the block.
    assert "stopLossEnabled" in after


@pytest.mark.asyncio
async def test_a_limit_looser_than_the_accounts_own_value_is_clamped_to_it(
    authenticated_client, db_session, pro_user
):
    """The account's own limit still wins: a client may tighten, never loosen."""
    await _set_account_risk_management(db_session, pro_user.id, {"maxDrawdown": 5.0})

    response = await authenticated_client.post(
        STRATEGIES_URL,
        json=_save_payload(risk_settings={"max_drawdown_pct": 10.0}),
    )
    assert response.status_code == 201, response.text

    after = await _account_risk_management(db_session, pro_user.id)
    assert after["maxDrawdown"] == 5.0


# --- item 5: the contract is served over HTTP --------------------------------


@pytest.mark.asyncio
async def test_the_registry_endpoint_serves_the_servers_own_list_and_floor(
    authenticated_client,
):
    """The client must not carry a second, hand-written copy of the contract."""
    response = await authenticated_client.get(REGISTRY_URL)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["strategy_pick"]["available"] == list(CLIENT_SELECTABLE_STRATEGIES)
    assert data["server_floor"] == dict(SERVER_RISK_FLOOR)
    assert set(data["server_floor"].keys()) == set(SERVER_RISK_FLOOR.keys())


# --- item 3: the real editor payload survives the schema ---------------------


def test_the_real_editor_payload_keeps_its_risk_settings_through_the_schema():
    """The field is declared, so ``model_dump()`` cannot drop it.

    The assertion that the payload really lands on the MODEL branch is the
    point of this test: with the plain-dict branch the model is never
    validated, so a schema that dropped ``risk_settings`` would still pass.
    """
    payload = _real_editor_payload(
        risk_settings={"daily_loss_limit_pct": 2.0},
        strategy_params={"lookback_candles": 100},
    )
    model = schemas.StrategyConfigCreate(
        name="My strategy",
        description="written by the Strategies page",
        config_data=payload,
    )

    # The payload satisfies StrategyV2ConfigData, so THIS is the branch that
    # decides what is stored.
    assert isinstance(model.config_data, schemas.StrategyV2ConfigData)

    dumped = model.model_dump()
    assert dumped["config_data"]["risk_settings"]["daily_loss_limit_pct"] == 2.0
    assert dumped["config_data"]["strategy_params"]["lookback_candles"] == 100
    assert dumped["config_data"]["strategy_name"] == "VolumeBreakout"


@pytest.mark.asyncio
async def test_the_saved_row_keeps_the_clients_risk_settings(
    authenticated_client, db_session, pro_user
):
    """End to end: what the editor sends is what the database holds."""
    response = await authenticated_client.post(
        STRATEGIES_URL,
        json=_save_payload(
            risk_settings={"daily_loss_limit_pct": 2.0},
            strategy_params={"lookback_candles": 100},
        ),
    )
    assert response.status_code == 201, response.text

    stored = await _stored_configs(db_session, pro_user.id)
    assert len(stored) == 1
    config_data = stored[0].config_data
    assert config_data["risk_settings"]["daily_loss_limit_pct"] == 2.0
    assert config_data["strategy_params"]["lookback_candles"] == 100
