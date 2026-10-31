"""Gate 4, Part E: the risk limit has to be REAL, and the registry has to AGREE.

THE DEFECTS
===========
Five separate holes let a client's own configuration control the money:

D1 -- a client's per-trade risk bypassed the server limit entirely. The
      validator bounded only ``config_data["risk_settings"]``. The strategy
      parameter ``risk_pct_per_trade`` -- which is what the strategy
      ACTUALLY reads (``BaseStrategy._get_param``,
      bot_module/strategy.py:2386-2404) -- was never checked, and the
      RiskManager took the signal's own budget with no ceiling at all
      (bot_module/risk_manager.py:2439-2440). A client could ask for 99
      (a FRACTION) and risk 9900% of the balance on one trade.

D2 -- the API refused a strategy the bot could run. ``CompassStrategy``
      lives in its own module, so it is not a subclass yet when
      ``bot_module/strategy.py`` builds its registry at import time. The
      bot registered it BY NAME (bot_runner.py:37) and the API -- which
      never imports bot_runner -- refused it.

D3 -- the client's ``strategy_params`` never reached the strategies: they
      were validated, and then dropped.

D4 -- the merge could UNDO an account's own safety setting. An account
      whose ``riskPerTradePercent`` was 0 ("risk off",
      bot_module/risk_manager.py:181-183) got 1.0 written back by every
      strategy save, and a save that sent no risk settings at all rewrote
      the account's whole risk block with the server floor.

D5 -- the Risk Settings page could store a per-trade risk the server does
      not honour (api/routes/config.py:98-105).

WHAT THIS TEST PROVES
=====================
* every door a per-trade risk can come through is REFUSED (422) when it is
  looser than the server floor, and the STORED configuration never carries
  one;
* the RiskManager's own arithmetic caps the risk a signal asks for at the
  account's limit -- driven through the REAL ``assess_signal`` path, not a
  copy of it;
* the position budget is a FRACTION: an account limit of 1.0 PERCENT
  yields 0.01, not 1.0;
* an account that switched its risk OFF keeps it off, and a save that sent
  no risk settings changes nothing at all;
* the set the API offers is the set the runtime can run.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from api import models
from bot_module import config as bot_config
from bot_module.exchanges import ExchangeExecutor
from bot_module.risk_manager import RiskManager
from bot_module.strategy import STRATEGIES, SignalDirection, OrderMode, StrategySignal
from bot_module.strategy_registry import (
    CLIENT_SELECTABLE_STRATEGIES,
    SERVER_RISK_FLOOR,
    SERVER_RISK_PER_TRADE_PCT,
    ClientConfigError,
    apply_client_risk_config,
    validate_client_strategy_config,
)

STRATEGIES_URL = "/api/v1/strategies/config"
CONFIG_URL = "/api/v1/config"

#: The server's per-trade risk floor, in the two units it is expressed in.
#: The account block is in PERCENT; the strategy parameter is a FRACTION.
FLOOR_PERCENT = float(bot_config.DEFAULT_RISK_PER_TRADE_PERCENT)
FLOOR_FRACTION = FLOOR_PERCENT / 100.0


# --- helpers ----------------------------------------------------------------


def _editor_payload(**overrides: Any) -> Dict[str, Any]:
    """The payload the strategy editor really sends.

    It MATTERS that this payload satisfies ``StrategyV2ConfigData``: the
    schema field is a union, so a payload that does not satisfy the model
    falls through to the plain-dict branch, and the model branch is the one
    that decides what is actually stored.
    """
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


def _save_payload(**config_data_overrides: Any) -> Dict[str, Any]:
    return {
        "name": "Gate 4 risk limit",
        "description": "written by the strategy editor",
        "config_data": _editor_payload(**config_data_overrides),
        "symbol_selection_mode": "STATIC",
        "symbols": ["BTCUSDT"],
        "use_ml_confirmation": False,
        "foundation_weights": None,
    }


async def _stored_configs(db_session, user_id: int) -> List[Any]:
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
    config_row = result.scalars().first()
    assert config_row is not None, "the account has no AppConfig row"
    return dict(config_row.risk_management or {})


async def _set_account_risk_management(
    db_session, user_id: int, values: Dict[str, Any]
) -> None:
    db_session.expire_all()
    result = await db_session.execute(
        select(models.AppConfig).where(models.AppConfig.user_id == user_id)
    )
    config_row = result.scalars().first()
    assert config_row is not None, "the account has no AppConfig row"
    merged = dict(config_row.risk_management or {})
    merged.update(values)
    config_row.risk_management = merged
    await db_session.commit()


# --- D1 / F2: every door a per-trade risk comes through is REFUSED ----------


@pytest.mark.parametrize(
    "config_data_overrides,expected_text",
    [
        ({"risk_pct_per_trade": 99}, "config_data.risk_pct_per_trade"),
        ({"config": {"risk_pct_per_trade": 99}}, "config_data.config.risk_pct_per_trade"),
        (
            {"strategy_params": {"risk_pct_per_trade": 99}},
            "config_data.strategy_params.risk_pct_per_trade",
        ),
    ],
)
def test_a_per_trade_risk_looser_than_the_floor_is_refused(
    config_data_overrides, expected_text
):
    """The floor applies to the parameter, not only to ``risk_settings``.

    The value is a FRACTION (0.01 is 1%); the floor is a PERCENT (1.0 is
    1%). 99 means 9900% of the balance.
    """
    payload = {"config_data": dict({"strategy_name": "VolumeBreakout"}, **config_data_overrides)}
    with pytest.raises(ClientConfigError) as excinfo:
        validate_client_strategy_config(payload)
    assert any(expected_text in issue for issue in excinfo.value.issues), (
        excinfo.value.issues
    )


@pytest.mark.parametrize(
    "config_data_overrides",
    [
        {"risk_pct_per_trade": 99},
        {"config": {"risk_pct_per_trade": 99}},
        {"strategy_params": {"risk_pct_per_trade": 99}},
    ],
)
@pytest.mark.asyncio
async def test_the_api_refuses_a_per_trade_risk_looser_than_the_floor(
    authenticated_client, db_session, pro_user, config_data_overrides
):
    """The refusal is a 422 at the request boundary, and nothing is written."""
    response = await authenticated_client.post(
        STRATEGIES_URL, json=_save_payload(**config_data_overrides)
    )

    assert response.status_code == 422, response.text
    body = response.json()
    issues = body["error"]["issues"]
    assert any("risk_pct_per_trade" in issue for issue in issues), issues

    assert await _stored_configs(db_session, pro_user.id) == []


def test_a_per_trade_risk_tighter_than_the_floor_is_accepted():
    """Tightening is still allowed -- the floor is a ceiling, not a target."""
    payload = {
        "config_data": {
            "strategy_name": "VolumeBreakout",
            "risk_pct_per_trade": FLOOR_FRACTION / 2.0,
        }
    }
    config = validate_client_strategy_config(payload)
    assert config.risk_pct_per_trade == FLOOR_FRACTION / 2.0


def test_the_floor_is_compared_in_the_fraction_unit():
    """The floor is 1.0 PERCENT; a client value of 0.01 is the same limit.

    A client that sends exactly the floor is accepted; one percent more is
    refused. If the comparison were done in the wrong unit, one of these
    two would be wrong.
    """
    at_floor = {
        "config_data": {
            "strategy_name": "VolumeBreakout",
            "risk_pct_per_trade": FLOOR_FRACTION,
        }
    }
    validate_client_strategy_config(at_floor)  # does not raise

    above_floor = {
        "config_data": {
            "strategy_name": "VolumeBreakout",
            "risk_pct_per_trade": FLOOR_FRACTION * 1.01,
        }
    }
    with pytest.raises(ClientConfigError):
        validate_client_strategy_config(above_floor)


# --- D1 / F1: the RiskManager's own arithmetic caps the signal's budget -----


@pytest_asyncio.fixture
async def risk_manager_for_account(db_session, test_user):
    """A RiskManager whose account allows 1% of the balance per trade."""
    executor = AsyncMock(spec=ExchangeExecutor)
    executor.get_account_balance.return_value = {
        "USDT": {"free": "10000.00", "locked": "0.00"}
    }
    manager = RiskManager(
        executor=executor,
        paper_executor=executor,
        user_id=test_user.id,
        db_session=db_session,
        user_settings={
            "risk_management": {
                "riskPerTradePercent": 1.0,
                "dailyMaxLossPercent": 5.0,
                "maxDrawdown": 20.0,
                "maxConsecutiveLosses": 10,
            }
        },
    )
    await manager.initialize()
    assert manager.stats.current_balance == 10000.0
    return manager


def _signal_with_risk(**kwargs: Any) -> StrategySignal:
    return StrategySignal(
        strategy_name="TestStrat",
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        trigger_price=50000.0,
        stop_loss=49500.0,
        take_profit=51000.0,
        mode=OrderMode.MARKET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_the_risk_manager_never_risks_more_than_the_account_limit(
    risk_manager_for_account, monkeypatch
):
    """A signal that asks for 99 (9900%) may not get it.

    This drives the REAL ``assess_signal`` path: the planned risk it
    returns is the money the trade would have risked.
    """
    # The notional cap is a separate limit and is not what is measured here.
    monkeypatch.setattr(
        bot_config, "MAX_REAL_POSITION_SIZE_PCT_BALANCE", 100.0, raising=True
    )

    signal = _signal_with_risk(risk_pct=99.0)
    approved, _quantity, planned_risk, _reason = (
        await risk_manager_for_account.assess_signal(
            signal, {"minQty": 0.0001, "maxQty": 9000.0, "stepSize": 0.00001}, 10.0
        )
    )

    balance = risk_manager_for_account.stats.current_balance
    account_limit = balance * risk_manager_for_account.risk_per_trade
    assert planned_risk is not None
    assert planned_risk <= account_limit + 1e-9, (
        f"the signal asked for 9900% of the balance and the manager planned "
        f"${planned_risk:.2f} against an account limit of ${account_limit:.2f}"
    )
    assert approved is True


@pytest.mark.asyncio
async def test_the_risk_manager_clamps_a_fixed_usd_risk_budget(
    risk_manager_for_account, monkeypatch
):
    """The sibling channel -- a fixed USD budget -- is capped the same way.

    ``signal.risk_usd`` is reachable from the client too: the visual
    builder's ``initialization`` action takes a ``risk_value`` and a
    ``risk_type`` (``fixed_usd``), and the RiskManager used it with no
    ceiling at all.
    """
    monkeypatch.setattr(
        bot_config, "MAX_REAL_POSITION_SIZE_PCT_BALANCE", 100.0, raising=True
    )

    signal = _signal_with_risk(risk_usd=1_000_000.0)
    _approved, _quantity, planned_risk, _reason = (
        await risk_manager_for_account.assess_signal(
            signal, {"minQty": 0.0001, "maxQty": 9000.0, "stepSize": 0.00001}, 10.0
        )
    )

    balance = risk_manager_for_account.stats.current_balance
    account_limit = balance * risk_manager_for_account.risk_per_trade
    assert planned_risk is not None
    assert planned_risk <= account_limit + 1e-9, (
        f"a fixed budget of $1,000,000 was planned as ${planned_risk:.2f} "
        f"against an account limit of ${account_limit:.2f}"
    )


@pytest.mark.asyncio
async def test_a_signal_that_asks_for_less_is_still_honoured(
    risk_manager_for_account, monkeypatch
):
    """The guard clamps; it does not flatten every budget to the ceiling."""
    monkeypatch.setattr(
        bot_config, "MAX_REAL_POSITION_SIZE_PCT_BALANCE", 100.0, raising=True
    )

    signal = _signal_with_risk(risk_pct=FLOOR_FRACTION / 2.0)
    _approved, _quantity, planned_risk, _reason = (
        await risk_manager_for_account.assess_signal(
            signal, {"minQty": 0.0001, "maxQty": 9000.0, "stepSize": 0.00001}, 10.0
        )
    )

    balance = risk_manager_for_account.stats.current_balance
    assert planned_risk is not None
    assert planned_risk == pytest.approx(balance * FLOOR_FRACTION / 2.0)


# --- F3: the position budget is a FRACTION, not a PERCENT --------------------


def test_the_position_budget_is_a_fraction_not_a_percent():
    """An account limit of 1.0 PERCENT must yield 0.01, not 1.0.

    The strategy parameter and the RiskManager work in FRACTIONS
    (bot_module/config.py:1131, bot_module/risk_manager.py:2442). A budget
    of 1.0 in that unit is 100% of the balance -- fifty times looser than
    the 0.01 default it replaced.
    """
    applied = apply_client_risk_config(
        None, account_risk_settings={"riskPerTradePercent": 1.0}
    )
    budget = applied["strategy_params"]["risk_pct_per_trade"]
    assert budget == pytest.approx(0.01)
    assert budget == pytest.approx(FLOOR_FRACTION)


def test_a_client_position_size_is_also_a_fraction():
    """``position_size_pct`` is a PERCENT; the budget it produces is not."""
    config = validate_client_strategy_config(
        {
            "config_data": {
                "strategy_name": "VolumeBreakout",
                "risk_settings": {"position_size_pct": 0.5},
            }
        }
    )
    applied = apply_client_risk_config(
        config, account_risk_settings={"riskPerTradePercent": 1.0}
    )
    assert applied["strategy_params"]["risk_pct_per_trade"] == pytest.approx(0.005)


# --- D4 / F6: the merge only touches what the client actually sent -----------


@pytest.mark.asyncio
async def test_an_account_with_risk_off_keeps_risk_off_after_a_strategy_save(
    authenticated_client, db_session, pro_user
):
    """``riskPerTradePercent = 0`` means "risk off" and must survive a save.

    bot_module/risk_manager.py:181-183 reads it with ``or 0.0``, so 0 is a
    real setting, not an unset one. The merge used to treat it as unset and
    write the floor (1.0) back -- switching a client's risk back ON.
    """
    await _set_account_risk_management(
        db_session, pro_user.id, {"riskPerTradePercent": 0.0}
    )
    assert (
        await _account_risk_management(db_session, pro_user.id)
    )["riskPerTradePercent"] == 0.0

    response = await authenticated_client.post(
        STRATEGIES_URL,
        json=_save_payload(risk_settings={"max_drawdown_pct": 3.0}),
    )
    assert response.status_code == 201, response.text

    after = await _account_risk_management(db_session, pro_user.id)
    assert after["riskPerTradePercent"] == 0.0, (
        "a strategy save turned the account's risk back on"
    )
    assert after["maxDrawdown"] == 3.0


@pytest.mark.asyncio
async def test_a_save_with_no_risk_settings_leaves_the_account_untouched(
    authenticated_client, db_session, pro_user
):
    """No risk settings sent means NOTHING is written to the account.

    The merge used to run on every save and rewrite the account's whole
    risk block with the server floor, which silently undid the account's
    own tighter limits.
    """
    await _set_account_risk_management(
        db_session,
        pro_user.id,
        {"riskPerTradePercent": 0.5, "maxDrawdown": 4.0, "maxConcurrentTrades": 2},
    )
    before = await _account_risk_management(db_session, pro_user.id)

    response = await authenticated_client.post(STRATEGIES_URL, json=_save_payload())
    assert response.status_code == 201, response.text

    after = await _account_risk_management(db_session, pro_user.id)
    assert after == before, (
        f"the account's risk block was rewritten: {before} -> {after}"
    )


# --- D2 / F5: the API's set is the runtime's set ----------------------------


def test_the_api_selectable_set_is_the_runtime_registry():
    """No strategy the runtime can run may be refused by the API."""
    assert set(CLIENT_SELECTABLE_STRATEGIES) == set(STRATEGIES.keys())
    assert "CompassStrategy" in CLIENT_SELECTABLE_STRATEGIES
    assert "CompassStrategy" in STRATEGIES


def test_compass_strategy_went_through_the_verification():
    """It is registered VERIFIED, not on the strength of its name."""
    from bot_module.strategy import _verify_registry_candidate
    from bot_module.compass_strategy import CompassStrategy

    assert STRATEGIES["CompassStrategy"] is CompassStrategy
    assert _verify_registry_candidate(CompassStrategy) is None


def test_a_fresh_interpreter_import_registers_compass_strategy():
    """The API process must end up with the same registry as the bot.

    Measured in a FRESH interpreter, because the defect was an import-order
    defect: the registry is built at import time, and the API process never
    imports bot_runner.
    """
    script = (
        "import bot_module.strategy_registry as sr;"
        "print('COMPASS_IN_REGISTRY', 'CompassStrategy' in sr.STRATEGIES);"
        "print('COMPASS_SELECTABLE', 'CompassStrategy' in sr.CLIENT_SELECTABLE_STRATEGIES)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert "COMPASS_IN_REGISTRY True" in result.stdout, result.stdout
    assert "COMPASS_SELECTABLE True" in result.stdout, result.stdout


@pytest.mark.asyncio
async def test_the_api_accepts_a_client_pick_of_compass_strategy(
    authenticated_client, db_session, pro_user
):
    """The regression was that the API REFUSED a strategy the bot can run."""
    response = await authenticated_client.post(
        STRATEGIES_URL, json=_save_payload(strategy_name="CompassStrategy")
    )
    assert response.status_code == 201, response.text

    stored = await _stored_configs(db_session, pro_user.id)
    assert len(stored) == 1
    assert stored[0].config_data["strategy_name"] == "CompassStrategy"


# --- F4: what is persisted is what is enforced ------------------------------


@pytest.mark.asyncio
async def test_the_stored_config_carries_no_per_trade_risk_above_the_floor(
    authenticated_client, db_session, pro_user
):
    """The stored configuration is bounded, in every place it can be read."""
    response = await authenticated_client.post(
        STRATEGIES_URL,
        json=_save_payload(
            strategy_params={"lookback_candles": 100, "risk_pct_per_trade": 0.005}
        ),
    )
    assert response.status_code == 201, response.text

    stored = await _stored_configs(db_session, pro_user.id)
    assert len(stored) == 1
    config_data = stored[0].config_data

    # The three doors the strategy reads it through.
    assert config_data["risk_pct_per_trade"] <= FLOOR_FRACTION
    assert (
        config_data["strategy_params"]["risk_pct_per_trade"] <= FLOOR_FRACTION
    )
    nested = config_data.get("config")
    if isinstance(nested, dict) and "risk_pct_per_trade" in nested:
        assert nested["risk_pct_per_trade"] <= FLOOR_FRACTION


@pytest.mark.asyncio
async def test_the_clients_strategy_parameters_reach_the_stored_config(
    authenticated_client, db_session, pro_user
):
    """The parameters are stored where ``_get_param`` reads them.

    ``BaseStrategy._get_param`` (bot_module/strategy.py:2386-2404) reads the
    top level of ``config_data`` first, and the controller hands the
    strategy the ``config_data`` verbatim
    (bot_module/controller.py:2386-2391). They used to be validated and
    then dropped, so the strategy ran with the shipped defaults.
    """
    response = await authenticated_client.post(
        STRATEGIES_URL,
        json=_save_payload(strategy_params={"lookback_candles": 100}),
    )
    assert response.status_code == 201, response.text

    stored = await _stored_configs(db_session, pro_user.id)
    config_data = stored[0].config_data
    assert config_data["lookback_candles"] == 100
    assert config_data["strategy_params"]["lookback_candles"] == 100


@pytest.mark.asyncio
async def test_a_strategy_pick_is_stored_with_the_pick_the_client_made(
    authenticated_client, db_session, pro_user
):
    """The stored pick is the validated one."""
    response = await authenticated_client.post(
        STRATEGIES_URL, json=_save_payload(strategy_name="FakeBreakout")
    )
    assert response.status_code == 201, response.text

    stored = await _stored_configs(db_session, pro_user.id)
    assert stored[0].config_data["strategy_name"] == "FakeBreakout"


# --- D5 / F7: the Risk Settings page cannot store a limit the server ignores -


@pytest.mark.asyncio
async def test_the_risk_settings_page_cannot_store_an_unenforceable_limit(
    authenticated_client, db_session, pro_user
):
    """A per-trade risk above the server floor is refused, not stored."""
    await _set_account_risk_management(
        db_session, pro_user.id, {"riskPerTradePercent": 1.0}
    )
    before = await _account_risk_management(db_session, pro_user.id)

    response = await authenticated_client.put(
        CONFIG_URL,
        json={
            "risk_management": {
                "maxDrawdown": 15.5,
                "maxConcurrentTrades": 7,
                "stopLossEnabled": False,
                "riskPerTradePercent": 50.0,
            }
        },
    )
    assert response.status_code == 422, response.text

    after = await _account_risk_management(db_session, pro_user.id)
    assert after["riskPerTradePercent"] == before["riskPerTradePercent"]


@pytest.mark.asyncio
async def test_the_risk_settings_page_can_still_tighten_the_limit(
    authenticated_client, db_session, pro_user
):
    """The bound is a ceiling: a tighter limit is still accepted."""
    response = await authenticated_client.put(
        CONFIG_URL,
        json={
            "risk_management": {
                "maxDrawdown": 15.5,
                "maxConcurrentTrades": 7,
                "stopLossEnabled": False,
                "riskPerTradePercent": 0.5,
            }
        },
    )
    assert response.status_code == 200, response.text

    after = await _account_risk_management(db_session, pro_user.id)
    assert after["riskPerTradePercent"] == 0.5


# --- the constants the tests above rely on ----------------------------------


def test_the_server_floor_is_the_one_the_registry_advertises():
    """The floor the API serves and the floor the validator enforces agree."""
    assert SERVER_RISK_FLOOR["risk_per_trade_pct"] == FLOOR_PERCENT
    assert SERVER_RISK_PER_TRADE_PCT == FLOOR_PERCENT
    assert FLOOR_FRACTION == pytest.approx(0.01)
