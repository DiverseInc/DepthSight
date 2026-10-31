"""The client contract for the strategy pick and the risk settings.

WHY THIS MODULE EXISTS
=======================
Two product defects met here:

1. The strategy list was decorative. Nine named strategies existed with real
   implementations but were never registered, so a client who picked one got
   a product that could not start it. The registry in ``bot_module.strategy``
   now verifies every candidate before it is allowed in.

2. The client's pick and risk settings never reached the server. The Risk
   page on the Strategies screen kept the pick and the risk numbers in LOCAL
   component state and never POSTed them, so the bot ran with whatever the
   account happened to have configured. This module is the missing contract:
   it defines the canonical fields, validates them, refuses any value that
   would make the server's guard LOOSER, and produces the exact values the
   bot already reads.

THE RULE THAT MATTERS
=====================
A client may TIGHTEN a risk limit. A client may never LOOSEN one past what
the server enforces. The server floor below is the ceiling for every
client-settable risk number, and ``apply_client_risk_config`` never returns a
value above it -- and never above the account's own current value either.

WHAT THE FRONTEND SHOULD SEND
==============================
POST /strategies (or PUT /strategies/{id}) with this body::

    {
      "name": "My VolumeBreakout",
      "description": "optional",
      "config_data": {
        "strategy_name": "VolumeBreakout",
        "strategy_params": { "lookback_candles": 100 },
        "risk_settings": {
          "risk_per_trade_pct": 0.5,
          "max_drawdown_pct": 10.0,
          "daily_loss_limit_pct": 2.0,
          "position_size_pct": 0.5,
          "max_stop_distance_pct": 2.0,
          "max_consecutive_losses": 5
        },
        "symbol_selection_mode": "manual",
        "symbols": ["BTCUSDT"]
      }
    }

``config_data["strategy_name"]`` is the PICK. It is the key the bot already
reads when it starts a strategy (bot_module/controller.py:2233 reads
``config_data.get("strategy_name")``), and it is already a field of the
existing ``StrategyV2ConfigData`` schema (api/schemas.py:1727), so no schema
change is needed to carry it.

``config_data["risk_settings"]`` is the RISK block. Every key is optional;
omitting one means "leave the account's current value alone".

``config_data["strategy_params"]`` is the per-strategy parameter set. It is
merged into the params the strategy instance is constructed with.

WHERE EACH RISK NUMBER ENDS UP
===============================
The bot reads its limits from the account settings, not from the strategy
config (bot_module/risk_manager.py:179 reads
``user_settings["risk_management"]``). So the API layer must merge the
applied values back into the account's ``risk_management`` block --
``apply_client_risk_config`` returns them ready to merge::

    risk_per_trade_pct      -> riskPerTradePercent   (account level)
    max_drawdown_pct        -> maxDrawdown           (account level)
    daily_loss_limit_pct    -> dailyMaxLossPercent   (account level)
    max_stop_distance_pct   -> maxStopDistancePct    (account level)
    max_consecutive_losses  -> maxConsecutiveLosses  (account level)
    position_size_pct       -> strategy param risk_pct_per_trade (per strategy)

``position_size_pct`` is per strategy because the strategy signal carries its
own risk budget (``signal.risk_pct``), and the RiskManager uses that
override INSTEAD of the account value for that trade
(bot_module/risk_manager.py:2439). That is exactly why it is clamped to the
account's risk per trade: a strategy-level budget above the account budget
would silently raise the account's exposure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type

from bot_module import config
from bot_module.strategy import STRATEGIES, BaseStrategy

logger = logging.getLogger(__name__)


class ClientConfigError(ValueError):
    """Raised when a client payload is not acceptable.

    ``issues`` holds one human-readable line per problem, so the API layer can
    return all of them at once instead of making the client guess.
    """

    def __init__(self, issues: Any) -> None:
        if isinstance(issues, str):
            issues = [issues]
        self.issues: List[str] = list(issues)
        super().__init__("; ".join(self.issues))


# --- the server floor -------------------------------------------------------
#
# These are the server's own limits. They are read from bot_module/config.py
# so they track the configuration instead of drifting away from it. A client
# value may be LOWER than the floor (tighter) and never higher (looser).

#: Maximum loss, as a percentage of the balance, allowed in one day.
#: The bot's own gate: bot_module/risk_manager.py:214-221 reads
#: ``dailyMaxLossPercent`` and compares it at risk_manager.py:2246.
SERVER_DAILY_LOSS_LIMIT_PCT: float = float(config.DEFAULT_DAILY_MAX_LOSS_PERCENT)

#: Maximum total drawdown, as a percentage of the balance.
#: bot_module/risk_manager.py:210-213 reads ``maxDrawdown`` and compares it at
#: risk_manager.py:2238.
SERVER_MAX_DRAWDOWN_PCT: float = float(config.DEFAULT_MAX_DRAWDOWN_PERCENT)

#: Maximum risk per trade, as a percentage of the balance.
#: bot_module/risk_manager.py:222-229 reads ``riskPerTradePercent``.
SERVER_RISK_PER_TRADE_PCT: float = float(config.DEFAULT_RISK_PER_TRADE_PERCENT)

#: Maximum number of consecutive losing trades before trading stops.
#: bot_module/risk_manager.py:230-232 reads ``maxConsecutiveLosses`` and
#: compares it at risk_manager.py:2249.
SERVER_MAX_CONSECUTIVE_LOSSES: int = int(config.DEFAULT_MAX_CONSECUTIVE_LOSSES)

#: Maximum distance of the stop-loss from the entry price, as a percentage.
#: The RiskManager's own default: bot_module/risk_manager.py:184-186 reads
#: ``maxStopDistancePct`` with a 5.0 fallback.
SERVER_MAX_STOP_DISTANCE_PCT: float = 5.0

#: The client may not set any risk number above these values.
SERVER_RISK_FLOOR: Dict[str, float] = {
    "risk_per_trade_pct": SERVER_RISK_PER_TRADE_PCT,
    "max_drawdown_pct": SERVER_MAX_DRAWDOWN_PCT,
    "daily_loss_limit_pct": SERVER_DAILY_LOSS_LIMIT_PCT,
    "position_size_pct": SERVER_RISK_PER_TRADE_PCT,
    "max_stop_distance_pct": SERVER_MAX_STOP_DISTANCE_PCT,
    "max_consecutive_losses": float(SERVER_MAX_CONSECUTIVE_LOSSES),
}

#: The risk number -> the key the RiskManager reads it from.
RISK_FIELD_TO_RM_KEY: Dict[str, str] = {
    "risk_per_trade_pct": "riskPerTradePercent",
    "max_drawdown_pct": "maxDrawdown",
    "daily_loss_limit_pct": "dailyMaxLossPercent",
    "max_stop_distance_pct": "maxStopDistancePct",
    "max_consecutive_losses": "maxConsecutiveLosses",
}

#: The strategy parameter that carries the per-trade position budget.
POSITION_SIZE_STRATEGY_PARAM: str = "risk_pct_per_trade"

#: The strategy names a client may pick. This is the registry, so a name that
#: is not here cannot be started and is not offered.
CLIENT_SELECTABLE_STRATEGIES: Tuple[str, ...] = tuple(sorted(STRATEGIES.keys()))

#: The pick the server uses when a client made none.
#:
#: It is the schema's own default (``api/schemas.py``: ``StrategyV2ConfigData.
#: strategy_name``) and the controller's fallback (``bot_module/controller.py``
#: assigns "VisualBuilderStrategy" when ``config_data`` has no
#: ``strategy_name``). A client that made no pick therefore gets exactly the
#: strategy it gets today -- it is NOT refused, because refusing it would
#: break every configuration that was saved before the pick existed.
DEFAULT_CLIENT_STRATEGY_NAME: str = "VisualBuilderStrategy"

#: The strategy modes a client may ask for.
CLIENT_ALLOWED_MODES: Tuple[str, ...] = ("paper", "live")

#: The symbol selection modes the product understands.
CLIENT_ALLOWED_SYMBOL_SELECTION_MODES: Tuple[str, ...] = ("manual", "auto", "all")


@dataclass(frozen=True)
class ClientStrategyConfig:
    """A validated client strategy pick plus its risk settings."""

    strategy_name: str
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    risk_settings: Dict[str, Any] = field(default_factory=dict)
    symbol_selection_mode: Optional[str] = None
    symbols: List[str] = field(default_factory=list)
    mode: str = "paper"

    def risk_field(self, name: str) -> Optional[float]:
        """The client's value for one risk field, or None when unset."""
        value = self.risk_settings.get(name)
        return None if value is None else float(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_risk_settings(raw: Any, issues: List[str]) -> Dict[str, Any]:
    """Validate the client's risk block against the server floor."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        issues.append("risk_settings must be an object")
        return {}

    unknown = sorted(set(raw.keys()) - set(SERVER_RISK_FLOOR.keys()))
    for name in unknown:
        issues.append(
            f"risk_settings.{name} is not a known risk setting; known settings are "
            f"{sorted(SERVER_RISK_FLOOR.keys())}"
        )

    validated: Dict[str, Any] = {}
    for name, value in raw.items():
        if name not in SERVER_RISK_FLOOR:
            continue
        if not _is_number(value):
            issues.append(f"risk_settings.{name} must be a number, got {value!r}")
            continue
        floor = SERVER_RISK_FLOOR[name]
        if float(value) <= 0:
            issues.append(f"risk_settings.{name} must be greater than 0, got {value!r}")
            continue
        if float(value) > floor:
            issues.append(
                f"risk_settings.{name}={value!r} is looser than the server limit of "
                f"{floor}. A client may tighten a risk limit but never loosen it."
            )
            continue
        validated[name] = int(value) if name == "max_consecutive_losses" else float(value)
    return validated


def _validate_strategy_params(raw: Any, issues: List[str]) -> Dict[str, Any]:
    """Validate the per-strategy parameter block."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        issues.append("strategy_params must be an object")
        return {}
    params: Dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            issues.append(f"strategy_params key {key!r} must be a non-empty string")
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            params[key] = value
            continue
        if isinstance(value, (list, tuple)):
            params[key] = list(value)
            continue
        if isinstance(value, Mapping):
            params[key] = dict(value)
            continue
        issues.append(
            f"strategy_params.{key} has an unsupported type {type(value).__name__}; "
            f"only JSON values are accepted"
        )
    return params


def validate_client_strategy_config(payload: Mapping[str, Any]) -> ClientStrategyConfig:
    """Validate the client's strategy pick and risk settings.

    Raises ``ClientConfigError`` with one line per problem. The rules are:

    * ``config_data.strategy_name`` must name a strategy that is in the
      registry. A name that is not registered cannot be started, so it is
      refused rather than stored and silently ignored. A configuration with
      NO pick at all is not refused: the server's own default applies
      (``DEFAULT_CLIENT_STRATEGY_NAME``), which is what the controller does
      with it anyway.
    * Every risk number must be a number greater than zero and no larger
      than the server floor for that field.
    * Unknown risk settings are refused. A setting the server does not know
      is a setting that would do nothing -- which is the defect this module
      exists to close.
    * ``symbol_selection_mode`` must be one of the known modes, and the
      "manual" mode requires at least one symbol.
    * ``mode`` must be "paper" or "live".
    """
    issues: List[str] = []
    if not isinstance(payload, Mapping):
        raise ClientConfigError("the request body must be an object")

    config_data = payload.get("config_data")
    if config_data is None:
        issues.append("config_data is required")
        config_data = {}
    if not isinstance(config_data, Mapping):
        raise ClientConfigError("config_data must be an object")
    config_data = dict(config_data)

    strategy_name = config_data.get("strategy_name")
    if strategy_name is None:
        # No pick was made. The server's own default applies -- the same
        # strategy the controller falls back to. A pick that is PRESENT must
        # be a strategy this server can run; a pick that is absent is not a
        # broken pick, it is no pick.
        strategy_name = DEFAULT_CLIENT_STRATEGY_NAME
    elif not isinstance(strategy_name, str) or not strategy_name.strip():
        issues.append("config_data.strategy_name is required")
        strategy_name = ""
    else:
        strategy_name = strategy_name.strip()
        if strategy_name not in STRATEGIES:
            issues.append(
                f"config_data.strategy_name={strategy_name!r} is not a strategy this "
                f"server can run; available strategies are "
                f"{list(CLIENT_SELECTABLE_STRATEGIES)}"
            )

    strategy_params = _validate_strategy_params(config_data.get("strategy_params"), issues)
    risk_settings = _validate_risk_settings(config_data.get("risk_settings"), issues)

    symbol_selection_mode = payload.get(
        "symbol_selection_mode", config_data.get("symbol_selection_mode")
    )
    if symbol_selection_mode is not None:
        if symbol_selection_mode not in CLIENT_ALLOWED_SYMBOL_SELECTION_MODES:
            issues.append(
                f"symbol_selection_mode={symbol_selection_mode!r} is not one of "
                f"{list(CLIENT_ALLOWED_SYMBOL_SELECTION_MODES)}"
            )

    raw_symbols = payload.get("symbols", config_data.get("symbols"))
    symbols: List[str] = []
    if raw_symbols is not None:
        if not isinstance(raw_symbols, (list, tuple)):
            issues.append("symbols must be a list of symbol names")
        else:
            for item in raw_symbols:
                if not isinstance(item, str) or not item.strip():
                    issues.append(f"symbols entry {item!r} must be a non-empty string")
                    continue
                symbols.append(item.strip().upper())
    if symbol_selection_mode == "manual" and not symbols:
        issues.append("symbol_selection_mode='manual' requires at least one symbol")

    mode = config_data.get("mode", payload.get("mode", "paper"))
    if mode not in CLIENT_ALLOWED_MODES:
        issues.append(f"mode={mode!r} is not one of {list(CLIENT_ALLOWED_MODES)}")

    if issues:
        raise ClientConfigError(issues)

    return ClientStrategyConfig(
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        risk_settings=risk_settings,
        symbol_selection_mode=symbol_selection_mode,
        symbols=symbols,
        mode=mode,
    )


def apply_client_risk_config(
    config_or_none: Optional[ClientStrategyConfig],
    account_risk_settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Turn a validated client config into the values the bot actually reads.

    Returns::

        {
          "risk_management": {
              "riskPerTradePercent": 0.5,
              "maxDrawdown": 10.0,
              "dailyMaxLossPercent": 2.0,
              "maxStopDistancePct": 2.0,
              "maxConsecutiveLosses": 5,
          },
          "strategy_params": {"risk_pct_per_trade": 0.5},
        }

    ``risk_management`` is the block the RiskManager reads
    (bot_module/risk_manager.py:179). It is meant to be MERGED into the
    account's ``risk_management`` settings, not to replace them: keys the
    client did not set keep the account's value.

    ``strategy_params`` is the per-strategy position budget. It is clamped to
    the account's risk per trade, because the strategy signal's own budget
    REPLACES the account budget for that trade
    (bot_module/risk_manager.py:2439-2443).

    The result is never looser than the server floor and never looser than
    the account's own current value.
    """
    account = dict(account_risk_settings or {})
    client = config_or_none
    risk_settings = dict(client.risk_settings) if client else {}

    applied_account: Dict[str, Any] = {}
    for field_name, rm_key in RISK_FIELD_TO_RM_KEY.items():
        floor = SERVER_RISK_FLOOR[field_name]
        current = account.get(rm_key)
        # The account's own value is itself capped by the floor: a value that
        # is already looser than the server limit must not be used as the
        # base for a client value either.
        ceiling = floor
        if _is_number(current) and float(current) > 0:
            ceiling = min(ceiling, float(current))
        requested = risk_settings.get(field_name)
        if requested is None:
            effective = ceiling
        else:
            effective = min(float(requested), ceiling)
        if field_name == "max_consecutive_losses":
            applied_account[rm_key] = int(effective)
        else:
            applied_account[rm_key] = float(effective)

    # The per-strategy position budget can never exceed the account's risk
    # per trade, because the signal-level budget replaces it for that trade.
    account_risk_per_trade = float(applied_account["riskPerTradePercent"])
    requested_position = risk_settings.get("position_size_pct")
    if requested_position is None:
        effective_position = account_risk_per_trade
    else:
        effective_position = min(float(requested_position), account_risk_per_trade)

    return {
        "risk_management": applied_account,
        "strategy_params": {POSITION_SIZE_STRATEGY_PARAM: effective_position},
    }


def resolve_client_strategy(
    config_or_none: Optional[ClientStrategyConfig],
) -> Optional[Type[BaseStrategy]]:
    """Resolve a validated client pick to the concrete registered class.

    This is the round trip: the name the client picked comes back as the
    class the engine will instantiate. It returns None when there is no
    config, so a caller can tell "not picked" from "picked but unknown"
    (the latter is impossible after validation -- it raises).
    """
    if config_or_none is None:
        return None
    return STRATEGIES.get(config_or_none.strategy_name)


def build_strategy_instance_params(
    config_or_none: Optional[ClientStrategyConfig],
) -> Dict[str, Any]:
    """Build the params the strategy instance is constructed with.

    The engine constructs a strategy with
    ``cls(params=..., contract_id=...)`` (bot_module/strategy.py), and the
    controller hands it the strategy's ``config_data``
    (bot_module/controller.py). The client's parameter set is merged in, and
    ``enabled`` is set to True when the client explicitly picked the strategy
    and did not say otherwise -- a picked strategy that is switched off is a
    strategy that does nothing.
    """
    if config_or_none is None:
        return {}
    params: Dict[str, Any] = dict(config_or_none.strategy_params)
    if "enabled" not in params:
        params["enabled"] = True
    return params


def build_config_data(config_or_none: Optional[ClientStrategyConfig]) -> Dict[str, Any]:
    """Build the ``config_data`` block to persist for a client pick.

    The result carries the pick under ``strategy_name`` -- the key the bot
    reads when it starts the strategy -- and the risk settings under
    ``risk_settings``.
    """
    if config_or_none is None:
        return {}
    config_data: Dict[str, Any] = {
        "strategy_name": config_or_none.strategy_name,
        "enabled": True,
    }
    if config_or_none.strategy_params:
        config_data["strategy_params"] = dict(config_or_none.strategy_params)
    if config_or_none.risk_settings:
        config_data["risk_settings"] = dict(config_or_none.risk_settings)
    if config_or_none.symbol_selection_mode:
        config_data["symbol_selection_mode"] = config_or_none.symbol_selection_mode
    if config_or_none.symbols:
        config_data["symbols"] = list(config_or_none.symbols)
    return config_data


def describe_client_contract() -> Dict[str, Any]:
    """A machine-readable description of the contract, for the API/frontend.

    The frontend agent can read this instead of guessing field names: it is
    generated from the same constants the validator uses, so it cannot drift.
    """
    return {
        "strategy_pick": {
            "field": "config_data.strategy_name",
            "type": "string",
            "required": False,
            "description": (
                "The pick. A name this server cannot run is refused. A "
                "configuration that carries NO pick is not refused: the "
                "server's own default applies, which is what the controller "
                "does with it anyway."
            ),
            "available": list(CLIENT_SELECTABLE_STRATEGIES),
        },
        "strategy_params": {
            "field": "config_data.strategy_params",
            "type": "object",
            "required": False,
            "description": (
                "Merged into the params the strategy instance is constructed with."
            ),
        },
        "risk_settings": {
            "field": "config_data.risk_settings",
            "type": "object",
            "required": False,
            "fields": {
                name: {
                    "type": "integer" if name == "max_consecutive_losses" else "number",
                    "unit": "percent",
                    "server_floor": SERVER_RISK_FLOOR[name],
                    "risk_manager_key": RISK_FIELD_TO_RM_KEY.get(
                        name, POSITION_SIZE_STRATEGY_PARAM
                    ),
                    "applies_to": (
                        "strategy" if name == "position_size_pct" else "account"
                    ),
                }
                for name in sorted(SERVER_RISK_FLOOR.keys())
            },
        },
        "symbol_selection_mode": {
            "field": "config_data.symbol_selection_mode",
            "type": "string",
            "required": False,
            "allowed": list(CLIENT_ALLOWED_SYMBOL_SELECTION_MODES),
        },
        "symbols": {
            "field": "config_data.symbols",
            "type": "array<string>",
            "required": "when symbol_selection_mode is 'manual'",
        },
        "mode": {
            "field": "config_data.mode",
            "type": "string",
            "required": False,
            "allowed": list(CLIENT_ALLOWED_MODES),
        },
        "server_floor": dict(SERVER_RISK_FLOOR),
        "rules": [
            "A risk value must be a number greater than 0.",
            "A risk value must not exceed the server floor for that field.",
            "A risk value must not exceed the account's current value.",
            "position_size_pct must not exceed the account's risk per trade.",
            "An unknown risk setting is refused rather than ignored.",
            "An unknown strategy name is refused rather than stored.",
        ],
    }
