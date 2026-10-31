"""Gate 4, Part B: the client's pick and risk settings must reach the server.

THE DEFECT
==========
The Risk settings on the Strategies page were LOCAL component state. The
client could move a slider, see the number change, and press Launch, and
nothing was POSTed: the bot ran with whatever the account happened to have
configured. The pick was in the same place.

THE CONTRACT BEING TESTED
=========================
``bot_module/strategy_registry.py`` is the contract:

* a validated pick resolves to a concrete registered strategy class;
* a risk number looser than the server's own limit is REFUSED, and the
  server's limit still applies;
* a risk number that tightens a limit is ACCEPTED, and the applied value
  is the client's value;
* an unknown strategy name is REFUSED;
* the applied values are the keys the bot actually reads
  (``user_settings["risk_management"]``).

These tests also push the payload through the real API schema
(``api.schemas.StrategyConfigCreate``), because that is where a pick can be
silently dropped: a schema that does not carry the field would store a
strategy config with no pick in it, and the bot would start the default
strategy instead of the one the client chose.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from api import schemas
from bot_module.strategy import STRATEGIES, BaseStrategy
from bot_module.strategy_registry import (
    SERVER_DAILY_LOSS_LIMIT_PCT,
    SERVER_MAX_DRAWDOWN_PCT,
    SERVER_MAX_STOP_DISTANCE_PCT,
    SERVER_RISK_FLOOR,
    SERVER_RISK_PER_TRADE_PCT,
    ClientConfigError,
    apply_client_risk_config,
    build_config_data,
    build_strategy_instance_params,
    describe_client_contract,
    resolve_client_strategy,
    validate_client_strategy_config,
)


def _payload(**risk_settings: Any) -> Dict[str, Any]:
    """A client payload as the frontend should send it."""
    return {
        "name": "My strategy",
        "description": "written by the Strategies page",
        "config_data": {
            "strategy_name": "VolumeBreakout",
            "strategy_params": {"lookback_candles": 100},
            "risk_settings": dict(risk_settings),
            "symbol_selection_mode": "manual",
            "symbols": ["BTCUSDT"],
        },
    }


# --- the risk floor ---------------------------------------------------------


def test_a_daily_loss_limit_looser_than_the_server_floor_is_refused():
    """The client may not loosen the server's own daily-loss gate."""
    payload = _payload(daily_loss_limit_pct=SERVER_DAILY_LOSS_LIMIT_PCT + 1.0)
    with pytest.raises(ClientConfigError) as excinfo:
        validate_client_strategy_config(payload)
    assert any("daily_loss_limit_pct" in issue for issue in excinfo.value.issues)


def test_the_server_floor_still_applies_when_the_client_sends_nothing():
    """With no client config the server's own limits are what is applied."""
    applied = apply_client_risk_config(None)
    risk_management = applied["risk_management"]
    assert risk_management["dailyMaxLossPercent"] == SERVER_DAILY_LOSS_LIMIT_PCT
    assert risk_management["maxDrawdown"] == SERVER_MAX_DRAWDOWN_PCT
    assert risk_management["riskPerTradePercent"] == SERVER_RISK_PER_TRADE_PCT
    assert risk_management["maxStopDistancePct"] == SERVER_MAX_STOP_DISTANCE_PCT


def test_a_refused_daily_loss_limit_leaves_the_server_floor_in_place():
    """A refused payload must not change what the server enforces."""
    before = apply_client_risk_config(None)["risk_management"]
    payload = _payload(daily_loss_limit_pct=25.0)
    with pytest.raises(ClientConfigError):
        validate_client_strategy_config(payload)
    after = apply_client_risk_config(None)["risk_management"]
    assert before == after
    assert after["dailyMaxLossPercent"] == SERVER_DAILY_LOSS_LIMIT_PCT


@pytest.mark.parametrize(
    "field_name,looser_value",
    [
        ("risk_per_trade_pct", SERVER_RISK_PER_TRADE_PCT + 0.5),
        ("max_drawdown_pct", SERVER_MAX_DRAWDOWN_PCT + 5.0),
        ("daily_loss_limit_pct", SERVER_DAILY_LOSS_LIMIT_PCT + 0.5),
        ("position_size_pct", SERVER_RISK_PER_TRADE_PCT + 0.5),
        ("max_stop_distance_pct", SERVER_MAX_STOP_DISTANCE_PCT + 1.0),
        ("max_consecutive_losses", int(SERVER_RISK_FLOOR["max_consecutive_losses"]) + 1),
    ],
)
def test_every_risk_field_is_capped_by_the_server_floor(field_name, looser_value):
    """No risk field may be set above the server's own limit."""
    payload = _payload(**{field_name: looser_value})
    with pytest.raises(ClientConfigError) as excinfo:
        validate_client_strategy_config(payload)
    assert any(field_name in issue for issue in excinfo.value.issues)


def test_a_tighter_daily_loss_limit_is_accepted_and_applied():
    """A client that tightens a limit gets that limit applied."""
    payload = _payload(daily_loss_limit_pct=2.0)
    config = validate_client_strategy_config(payload)
    assert config.risk_field("daily_loss_limit_pct") == 2.0

    applied = apply_client_risk_config(config)
    assert applied["risk_management"]["dailyMaxLossPercent"] == 2.0


def test_a_tighter_risk_block_is_applied_to_the_keys_the_bot_reads():
    """The applied values land on the keys the RiskManager reads."""
    payload = _payload(
        risk_per_trade_pct=0.4,
        max_drawdown_pct=8.0,
        daily_loss_limit_pct=1.5,
        max_stop_distance_pct=2.0,
        max_consecutive_losses=3,
    )
    config = validate_client_strategy_config(payload)
    applied = apply_client_risk_config(config)

    risk_management = applied["risk_management"]
    assert risk_management["riskPerTradePercent"] == 0.4
    assert risk_management["maxDrawdown"] == 8.0
    assert risk_management["dailyMaxLossPercent"] == 1.5
    assert risk_management["maxStopDistancePct"] == 2.0
    assert risk_management["maxConsecutiveLosses"] == 3
    assert applied["strategy_params"]["risk_pct_per_trade"] == 0.4


def test_a_tighter_limit_never_makes_the_server_looser_than_the_account():
    """The applied value is also capped by the account's own current value."""
    config = validate_client_strategy_config(_payload(daily_loss_limit_pct=4.0))
    applied = apply_client_risk_config(
        config, account_risk_settings={"dailyMaxLossPercent": 1.0}
    )
    assert applied["risk_management"]["dailyMaxLossPercent"] == 1.0


def test_the_position_size_is_capped_by_the_account_risk_per_trade():
    """A strategy-level budget above the account budget would raise exposure.

    The RiskManager uses the signal's own budget INSTEAD of the account
    budget for that trade, so a strategy-level value above the account's
    would silently widen the account's risk.
    """
    config = validate_client_strategy_config(_payload(position_size_pct=0.9))
    applied = apply_client_risk_config(
        config, account_risk_settings={"riskPerTradePercent": 0.25}
    )
    assert applied["strategy_params"]["risk_pct_per_trade"] == 0.25


def test_an_unknown_risk_setting_is_refused_rather_than_ignored():
    """A setting the server does not know is a setting that does nothing."""
    payload = _payload(daily_loss_limi_pct=2.0)
    with pytest.raises(ClientConfigError) as excinfo:
        validate_client_strategy_config(payload)
    assert any("daily_loss_limi_pct" in issue for issue in excinfo.value.issues)


def test_a_zero_or_negative_risk_value_is_refused():
    payload = _payload(daily_loss_limit_pct=0)
    with pytest.raises(ClientConfigError):
        validate_client_strategy_config(payload)


# --- the strategy pick ------------------------------------------------------


def test_an_unknown_strategy_name_is_refused():
    """A name the server cannot start must not be stored and ignored."""
    payload = _payload()
    payload["config_data"]["strategy_name"] = "NoSuchStrategy"
    with pytest.raises(ClientConfigError) as excinfo:
        validate_client_strategy_config(payload)
    assert any("NoSuchStrategy" in issue for issue in excinfo.value.issues)


def test_a_valid_pick_resolves_to_a_concrete_registered_class():
    """The round trip: the name the client picked comes back as a class."""
    config = validate_client_strategy_config(_payload())
    assert config.strategy_name == "VolumeBreakout"

    cls = resolve_client_strategy(config)
    assert cls is not None
    assert issubclass(cls, BaseStrategy)
    assert cls is STRATEGIES["VolumeBreakout"]
    assert cls.NAME == "VolumeBreakout"


def test_the_pick_is_recorded_in_the_config_data_the_bot_reads():
    """The bot reads config_data["strategy_name"] when it starts a strategy.

    bot_module/controller.py:2233 reads exactly this key, so if the pick is
    not stored under it the bot starts the default strategy instead of the
    one the client chose.
    """
    config = validate_client_strategy_config(_payload())
    config_data = build_config_data(config)
    assert config_data["strategy_name"] == "VolumeBreakout"
    assert config_data["enabled"] is True

    # The controller's own read.
    assert config_data.get("strategy_name") == "VolumeBreakout"


def test_the_pick_survives_the_real_api_schema():
    """The pick must not be dropped on the way into the database."""
    config = validate_client_strategy_config(_payload(daily_loss_limit_pct=2.0))
    config_data = build_config_data(config)

    model = schemas.StrategyConfigCreate(
        name="My strategy",
        description="written by the Strategies page",
        config_data=config_data,
    )

    stored = model.config_data
    if not isinstance(stored, dict):
        stored = stored.model_dump()
    assert stored["strategy_name"] == "VolumeBreakout"
    assert stored["strategy_params"]["lookback_candles"] == 100
    assert stored["risk_settings"]["daily_loss_limit_pct"] == 2.0


def test_the_picked_strategy_is_constructed_with_the_clients_parameters():
    """The parameter block reaches the strategy instance."""
    config = validate_client_strategy_config(_payload())
    params = build_strategy_instance_params(config)
    assert params["lookback_candles"] == 100
    assert params["enabled"] is True


def test_a_picked_strategy_is_enabled_even_when_the_shipped_default_is_off():
    """Several strategies ship with enabled=False.

    A client that explicitly picks one expects it to run. The registry
    forces enabled=True unless the client said otherwise.
    """
    from bot_module.strategy_registry import CLIENT_SELECTABLE_STRATEGIES

    payload = _payload()
    payload["config_data"]["strategy_name"] = "DensityBounce"
    config = validate_client_strategy_config(payload)
    assert "DensityBounce" in CLIENT_SELECTABLE_STRATEGIES

    params = build_strategy_instance_params(config)
    assert params["enabled"] is True

    from bot_module.strategy import create_strategy_instance

    instance = create_strategy_instance("DensityBounce", params=params)
    assert instance is not None
    assert instance.enabled is True


def test_the_contract_is_describable_for_the_frontend():
    """The API/frontend agent can read the contract instead of guessing."""
    contract = describe_client_contract()
    assert contract["strategy_pick"]["field"] == "config_data.strategy_name"
    assert "VolumeBreakout" in contract["strategy_pick"]["available"]
    fields = contract["risk_settings"]["fields"]
    assert fields["daily_loss_limit_pct"]["server_floor"] == SERVER_DAILY_LOSS_LIMIT_PCT
    assert (
        fields["daily_loss_limit_pct"]["risk_manager_key"] == "dailyMaxLossPercent"
    )
    for name, floor in SERVER_RISK_FLOOR.items():
        assert fields[name]["server_floor"] == floor
