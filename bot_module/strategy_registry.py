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

THE PER-TRADE RISK HAS THREE DOORS
==================================
``risk_settings`` is not the only way a client can set the per-trade risk.
The strategy parameter ``risk_pct_per_trade`` is read by
``BaseStrategy._get_param`` (bot_module/strategy.py:2386-2404) out of the
``config_data`` the controller hands the strategy VERBATIM
(bot_module/controller.py:2386-2391) -- at the top level first, then in the
nested ``config`` dict -- and the "Reverse*" strategies read it out of the
``strategy_params`` block (bot_module/strategy.py:12302-12335).

All three are bounded here, and all three are checked against the floor in
the same unit: the client's value is a FRACTION of the balance (0.01 is 1%),
the floor is a PERCENT (1.0 is 1%).
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type

from bot_module import config

# ``_verify_registry_candidate`` is the rule this module exists to enforce:
# a strategy is admitted on what it IS, never on the strength of its name.
# It lives in ``bot_module.strategy`` next to the registry it guards, and it
# is imported (not re-implemented) so the two can never drift apart.
from bot_module.strategy import STRATEGIES, BaseStrategy, _verify_registry_candidate

logger = logging.getLogger(__name__)


# --- the strategies that live in their own module ----------------------------
#
# ``bot_module/strategy.py`` builds its registry at IMPORT time from
# ``BaseStrategy.__subclasses__()``. A strategy that is defined in its OWN
# module is therefore not a subclass yet at that moment -- its module has not
# been imported -- so it was dropped from the registry with a warning.
#
# The consequence was that the API process and the bot process disagreed:
# ``bot_runner.py`` registered the strategy by name, so the bot could run it,
# while the API -- which never imports ``bot_runner`` -- refused it. The API
# is the request boundary, so a client could not even pick it.
#
# The fix is to make the registry itself complete, in a module BOTH processes
# import, and to put every candidate through the SAME verification every
# other candidate has to pass.
EXTERNAL_STRATEGY_MODULES: Tuple[str, ...] = ("bot_module.compass_strategy",)


def register_external_strategies() -> Dict[str, Type[BaseStrategy]]:
    """Register the strategies that live outside ``bot_module.strategy``.

    The registry is filled IN PLACE, and every candidate is verified with
    ``_verify_registry_candidate`` first: a strategy that cannot be started
    is REJECTED, with the reason, exactly like every other candidate.

    Safe to call more than once. A strategy that is already registered is
    left alone.
    """
    for module_name in EXTERNAL_STRATEGY_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            # A module that cannot be imported leaves its strategies out of
            # the registry -- and the API then refuses them, which is the
            # safe direction. Say so loudly: the bot cannot run them either.
            logger.error(
                f"Strategy module '{module_name}' could not be imported, so the "
                f"strategies it defines are NOT registered: {exc}"
            )
            continue
        for candidate in list(vars(module).values()):
            if not isinstance(candidate, type) or not issubclass(candidate, BaseStrategy):
                continue
            if candidate is BaseStrategy or candidate.__module__ != module.__name__:
                continue
            name = getattr(candidate, "NAME", None)
            if not isinstance(name, str) or not name:
                continue
            if STRATEGIES.get(name) is candidate:
                continue
            reason = _verify_registry_candidate(candidate)
            if reason:
                logger.error(
                    f"Strategy '{name}' REJECTED from the registry: {reason}. "
                    f"A client that picks it would get a strategy that cannot start."
                )
                continue
            STRATEGIES[name] = candidate
            logger.debug(
                f"Strategy '{name}' registered ({candidate.__module__}.{candidate.__name__})."
            )
    return STRATEGIES


# Run BEFORE ``CLIENT_SELECTABLE_STRATEGIES`` is computed below: the set a
# client may pick is the set the runtime can run, and it is computed from the
# registry, so the registry has to be complete first.
register_external_strategies()


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

#: The server's per-trade risk floor, expressed as a FRACTION of the balance.
#:
#: UNITS. This module has two units and they must never be mixed:
#:
#: * ``risk_settings`` is in PERCENT. ``risk_per_trade_pct = 0.5`` means
#:   0.5% of the balance, and the account block it is merged into
#:   (``riskPerTradePercent``) is read as a PERCENT by the RiskManager
#:   (bot_module/risk_manager.py:222-229 divides it by 100).
#: * ``risk_pct_per_trade`` -- the strategy parameter, and ``signal.risk_pct``
#:   -- is a FRACTION. ``0.005`` means 0.5% of the balance
#:   (bot_module/config.py:1131: ``DEFAULT_RISK_PER_TRADE_PERCENT / 100.0``;
#:   bot_module/risk_manager.py:2442 prints it back as ``* 100``).
#:
#: So ``SERVER_RISK_PER_TRADE_PCT`` (1.0) is the same limit as
#: ``SERVER_RISK_PER_TRADE_FRACTION`` (0.01). Every comparison of a client's
#: per-trade risk against the floor below is done in the FRACTION unit,
#: because that is the unit the client's value arrives in.
SERVER_RISK_PER_TRADE_FRACTION: float = SERVER_RISK_PER_TRADE_PCT / 100.0

#: The keys in the client's ``config_data`` that a strategy parameter may
#: NOT overwrite. They carry the pick, the risk block and the parameters
#: themselves, and the server writes the bounded values into them.
RESERVED_CONFIG_DATA_KEYS: frozenset = frozenset(
    {
        "strategy_name",
        "strategy_params",
        "risk_settings",
        "risk_pct_per_trade",
        "config",
        "symbols",
        "symbol_selection_mode",
        "mode",
    }
)

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
    #: The client's per-trade risk, as a FRACTION of the balance (0.01 is
    #: 1%), already bounded by the server floor. None when the client sent
    #: no per-trade risk at all.
    risk_pct_per_trade: Optional[float] = None

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


def _validate_client_risk_pct(
    value: Any, path: str, issues: List[str]
) -> Optional[float]:
    """Bound a client-supplied per-trade risk to the server floor.

    UNITS: the client's value is a FRACTION of the balance (0.01 == 1%); the
    server floor is a PERCENT (1.0 == 1%). The comparison is done in the
    FRACTION unit -- the unit the value arrives in, and the unit the
    RiskManager reads it in (bot_module/risk_manager.py:2442).

    Returns the validated fraction, or None when the value is absent or was
    refused (in which case an issue was appended).
    """
    if value is None:
        return None
    if not _is_number(value):
        issues.append(
            f"{path} must be a number (a fraction of the balance, so 0.01 is 1%), "
            f"got {value!r}"
        )
        return None
    fraction = float(value)
    if fraction < 0.0:
        issues.append(f"{path} must not be negative, got {value!r}")
        return None
    if fraction > SERVER_RISK_PER_TRADE_FRACTION:
        issues.append(
            f"{path}={value!r} is looser than the server limit of "
            f"{SERVER_RISK_PER_TRADE_FRACTION} of the balance "
            f"({SERVER_RISK_PER_TRADE_PCT}%). A client may tighten a risk limit "
            f"but never loosen it."
        )
        return None
    return fraction


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

    # The per-trade risk, in EVERY place it can hide.
    #
    # ``risk_settings`` is not the only channel. The strategy parameter
    # ``risk_pct_per_trade`` is read by ``BaseStrategy._get_param``
    # (bot_module/strategy.py:2386-2404) out of the ``config_data`` the
    # controller hands the strategy VERBATIM
    # (bot_module/controller.py:2386-2391) -- first at the top level, then in
    # the nested ``config`` dict -- and the "Reverse*" strategies read it out
    # of the ``strategy_params`` block (bot_module/strategy.py:12302-12335).
    #
    # So a client that put a per-trade risk in any of those three places
    # controlled the money the bot risked on the next trade. Each of them is
    # now bounded by the server floor, and a value that is looser than the
    # floor is REFUSED (422) rather than quietly clamped.
    risk_pct_candidates: List[float] = []

    top_level_risk_pct = _validate_client_risk_pct(
        config_data.get("risk_pct_per_trade"),
        "config_data.risk_pct_per_trade",
        issues,
    )
    if top_level_risk_pct is not None:
        risk_pct_candidates.append(top_level_risk_pct)

    nested_config = config_data.get("config")
    if isinstance(nested_config, Mapping):
        nested_risk_pct = _validate_client_risk_pct(
            nested_config.get("risk_pct_per_trade"),
            "config_data.config.risk_pct_per_trade",
            issues,
        )
        if nested_risk_pct is not None:
            risk_pct_candidates.append(nested_risk_pct)

    param_risk_pct = _validate_client_risk_pct(
        strategy_params.get("risk_pct_per_trade"),
        f"config_data.strategy_params.{POSITION_SIZE_STRATEGY_PARAM}",
        issues,
    )
    if param_risk_pct is not None:
        risk_pct_candidates.append(param_risk_pct)

    # The tightest value the client asked for is the one that is kept: the
    # strategy may read any of the three places, so the loosest one must not
    # be the one that survives.
    client_risk_pct: Optional[float] = (
        min(risk_pct_candidates) if risk_pct_candidates else None
    )

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
        risk_pct_per_trade=client_risk_pct,
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
          "strategy_params": {"risk_pct_per_trade": 0.005},
        }

    ``risk_management`` is the block the RiskManager reads
    (bot_module/risk_manager.py:179), in PERCENT. It is meant to be MERGED
    into the account's ``risk_management`` settings, not to replace them:
    keys the client did not set keep the account's value.

    ``strategy_params`` is the per-strategy position budget, in a FRACTION of
    the balance (0.005 is 0.5%) -- that is the unit the strategy parameter
    and the RiskManager work in. It is clamped to the account's risk per
    trade, because the strategy signal's own budget REPLACES the account
    budget for that trade (bot_module/risk_manager.py:2439-2443).

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
        if _is_number(current):
            if field_name == "risk_per_trade_pct" and float(current) == 0.0:
                # 0 is a REAL setting for the risk per trade, not an unset
                # one: bot_module/risk_manager.py:181-183 reads
                # ``riskPerTradePercent`` with ``or 0.0``, so 0 means "risk
                # off". Replacing it with the floor would silently turn a
                # client's "no risk" back on. It is kept, and a client may
                # only tighten it further (to 0).
                ceiling = 0.0
            else:
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
    #
    # UNITS. ``applied_account["riskPerTradePercent"]`` is a PERCENT (0.5 is
    # 0.5% of the balance); ``risk_pct_per_trade`` is a FRACTION (0.005 is
    # 0.5% of the balance) -- that is the unit ``_get_param`` and the
    # RiskManager work in (bot_module/config.py:1131, bot_module/
    # risk_manager.py:2442). The conversion happens HERE, once.
    account_risk_per_trade_pct = float(applied_account["riskPerTradePercent"])
    account_risk_per_trade_fraction = account_risk_per_trade_pct / 100.0
    requested_position = risk_settings.get("position_size_pct")
    if requested_position is None:
        effective_position = account_risk_per_trade_fraction
    else:
        # ``position_size_pct`` is a PERCENT, like every other risk setting.
        effective_position = min(
            float(requested_position) / 100.0, account_risk_per_trade_fraction
        )

    # The same budget can also arrive through the strategy parameter block,
    # where it is a FRACTION. Whichever channel the client used, the TIGHTER
    # one wins, and the result is never looser than the server floor.
    client_param_risk_pct = (
        client.strategy_params.get(POSITION_SIZE_STRATEGY_PARAM) if client else None
    )
    if _is_number(client_param_risk_pct):
        effective_position = min(effective_position, float(client_param_risk_pct))
    effective_position = min(effective_position, SERVER_RISK_PER_TRADE_FRACTION)

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


def build_bounded_config_data(
    original_config_data: Mapping[str, Any],
    config_or_none: Optional[ClientStrategyConfig],
    applied: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ``config_data`` the server STORES.

    What is persisted must be what is enforced. The request payload is not
    stored verbatim: the validated pick, the validated risk settings and the
    bounded per-trade risk are written over the client's own values.

    The client's strategy parameters are placed where the strategies actually
    read them. ``BaseStrategy._get_param`` (bot_module/strategy.py:2386-2404)
    looks in ``self._instance_params`` first and in
    ``self._instance_params["config"]`` second -- and the controller hands
    the strategy the ``config_data`` VERBATIM
    (bot_module/controller.py:2386-2391), so the parameters have to be at
    the TOP LEVEL of ``config_data``. The nested ``config`` dict is
    overwritten by the controller with ``config_data`` itself, so a value
    hidden in there would never be read.

    The ``strategy_params`` block is kept as well: it is the structured
    parameter set, and the "Reverse*" strategies read their parameters from
    it (bot_module/strategy.py:12302-12335).
    """
    bounded: Dict[str, Any] = dict(original_config_data or {})
    if config_or_none is None:
        return bounded

    applied = applied or {}
    applied_params = applied.get("strategy_params") or {}
    bounded_risk_pct = applied_params.get(POSITION_SIZE_STRATEGY_PARAM)
    if bounded_risk_pct is None:
        bounded_risk_pct = config_or_none.risk_pct_per_trade
    if bounded_risk_pct is None:
        bounded_risk_pct = SERVER_RISK_PER_TRADE_FRACTION
    bounded_risk_pct = min(
        float(bounded_risk_pct), SERVER_RISK_PER_TRADE_FRACTION
    )

    # The client's parameters, with the bounded per-trade risk on top of
    # them. A parameter may not overwrite a reserved key: the pick, the risk
    # block and the parameters themselves are written by the server.
    params: Dict[str, Any] = dict(config_or_none.strategy_params)
    params[POSITION_SIZE_STRATEGY_PARAM] = bounded_risk_pct
    for key, value in params.items():
        if key in RESERVED_CONFIG_DATA_KEYS:
            continue
        bounded[key] = value

    bounded["strategy_name"] = config_or_none.strategy_name
    bounded["enabled"] = bool(params.get("enabled", True))
    bounded["strategy_params"] = params
    bounded["risk_settings"] = dict(config_or_none.risk_settings)
    # The per-trade risk, in every place a strategy may read it from. The
    # stored configuration can never carry a per-trade risk looser than the
    # server floor.
    bounded[POSITION_SIZE_STRATEGY_PARAM] = bounded_risk_pct
    nested = bounded.get("config")
    if isinstance(nested, Mapping):
        nested = dict(nested)
        nested[POSITION_SIZE_STRATEGY_PARAM] = bounded_risk_pct
        bounded["config"] = nested
    return bounded


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
        "per_trade_risk": {
            "parameter": POSITION_SIZE_STRATEGY_PARAM,
            "unit": "fraction",
            "unit_note": (
                "0.01 means 1% of the balance. The risk_settings block is in "
                "PERCENT (1.0 means 1%); this parameter is a FRACTION."
            ),
            "server_floor": SERVER_RISK_PER_TRADE_FRACTION,
            "server_floor_percent": SERVER_RISK_PER_TRADE_PCT,
            "paths": [
                "config_data.risk_pct_per_trade",
                "config_data.config.risk_pct_per_trade",
                "config_data.strategy_params.risk_pct_per_trade",
            ],
            "description": (
                "The per-trade risk, in every place a strategy may read it "
                "from. A value looser than the server floor is REFUSED, not "
                "clamped."
            ),
        },
        "rules": [
            "A risk value must be a number greater than 0.",
            "A risk value must not exceed the server floor for that field.",
            "A risk value must not exceed the account's current value.",
            "position_size_pct must not exceed the account's risk per trade.",
            (
                "A per-trade risk (risk_pct_per_trade, a fraction of the "
                "balance) must not exceed "
                f"{SERVER_RISK_PER_TRADE_FRACTION} ({SERVER_RISK_PER_TRADE_PCT}%), "
                "in config_data, in config_data.config or in "
                "config_data.strategy_params."
            ),
            "An unknown risk setting is refused rather than ignored.",
            "An unknown strategy name is refused rather than stored.",
        ],
    }
