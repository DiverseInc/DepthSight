# File: bot_module/legacy_blocks_migration.py
"""
Translate the legacy `blocks` strategy format into the format the engine reads.

THE DEFECT THIS EXISTS TO REPAIR
--------------------------------
Seed templates used to ship their conditions as a `blocks` array:

    {"id": "rsi_entry", "type": "indicator", "indicator": "RSI",
     "period": 14, "condition": "crosses_above", "threshold": 55}

`bot_module/strategy.py` has never read `blocks`. It reads `entryConditions`,
`filters`, `initialization` and `positionManagement`. Every strategy created
from a pre-2026-10-05 seed therefore compiled to a None entry root, logged
"No entry conditions defined, no signal." on every candle, and sat in the
dashboard reporting status=running forever.

The seeds themselves were fixed in `5246390` (SEED_VERSION 1 -> 2), but
`strategy_configs.config_data` is a COPY taken when a user instantiates a
template, and nothing ever backfilled the copies already made. Those rows are
still carrying dead config. This module is how they get repaired.

WHY COPY THE TEMPLATE INSTEAD OF CONVERTING?
--------------------------------------------
Tempting, and wrong for these rows. The `rsi-breakout-v2` template's entry
matches exactly, but it has no exit, so swapping it in SILENTLY DELETES the
legacy strategy's RSI-75 exit and invents SL/TP the user never set. The legacy
`blocks` are fully specified -- indicator, period, comparison and threshold are
all present -- so a faithful conversion is possible and strictly better.

THREE RENAME TRAPS -- GET ANY ONE WRONG AND IT FAILS SILENTLY
-------------------------------------------------------------
A converted config that parses cleanly but never evaluates is exactly the
failure mode that shipped the dead seeds in the first place, so this module
refuses to guess:

  | legacy `condition`        | engine expects                       | where                        |
  |---------------------------|--------------------------------------|------------------------------|
  | "crosses_above"           | operator/direction "cross_above"     | api/crud.py:4266, :4300      |
  | "touches_lower"           | check_type "price_below_lower"       | condition_core.py:120        |
  | action "close_position"   | type "conditional_management"        | strategy.py:4307             |

Anything not in these tables raises `LossyConversionError`. Silence is the one
acceptable outcome for a repair tool.

KNOWN-LOSSY
-----------
`bollinger_bands_condition` accepts exactly four `check_type` values
(`price_below_lower`, `price_above_upper`, `width_gt`, `width_lt`). There is no
"price reached the middle band" check, so a legacy `reaches_middle` exit cannot
be expressed. That raises rather than substituting something that looks similar.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional


class LossyConversionError(ValueError):
    """Raised when a legacy block cannot be expressed without changing meaning.

    Subclasses ValueError so existing `except ValueError` handlers around config
    parsing keep working.
    """


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _num(value: Any, field: str, node_id: str) -> float:
    """Coerce a legacy numeric field, or raise LossyConversionError.

    `int()`/`float()` on a list or dict raises TypeError, which is NOT a
    ValueError and so would escape every `except ValueError` handler around
    config parsing. A malformed legacy row must fail the same way a lossy one
    does, not as an unexpected exception type.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        raise LossyConversionError(
            f"block {node_id!r}: {field} must be numeric, got {value!r}"
        ) from None


# --- vocabulary -------------------------------------------------------------
# Legacy condition words -> the exact strings the engine dispatches on.
# Anything absent from these tables is a bug or an unsupported feature, and is
# rejected rather than approximated.

_CROSS_RENAMES = {
    "crosses_above": "cross_above",
    "crosses_below": "cross_below",
}

_LEVEL_RENAMES = {
    "above": "gt",
    "greater_than": "gt",
    "below": "lt",
    "less_than": "lt",
}

# bollinger_bands_condition's complete, closed vocabulary (condition_core.py:120).
_BB_CHECK_TYPES = {
    "touches_lower": "price_below_lower",
    "below_lower": "price_below_lower",
    "touches_upper": "price_above_upper",
    "above_upper": "price_above_upper",
    "width_expands": "width_gt",
    "width_squeeze": "width_lt",
}

# Entries open a position; these actions exit one.
_EXIT_ACTIONS = {"close_position"}


def _operator_for(condition: str, node_id: str) -> str:
    if condition in _CROSS_RENAMES:
        return _CROSS_RENAMES[condition]
    if condition in _LEVEL_RENAMES:
        return _LEVEL_RENAMES[condition]
    raise LossyConversionError(
        f"block {node_id!r}: unknown RSI condition {condition!r}; "
        f"refusing to guess an operator"
    )


def _block_to_condition_node(block: Dict[str, Any]) -> Dict[str, Any]:
    """One legacy block -> one engine condition node, or raise."""
    node_id = block.get("id") or _uid("node")
    indicator = str(block.get("indicator", "")).upper()
    condition = str(block.get("condition", ""))

    if indicator == "RSI":
        period = block.get("period")
        threshold = block.get("threshold")
        if period is None or threshold is None:
            raise LossyConversionError(
                f"block {node_id!r}: RSI block needs both 'period' and 'threshold'"
            )
        return {
            "id": node_id,
            "type": "rsi_condition",
            "analysis_level": "minute_bar_filter",
            "params": {
                "period": int(_num(period, "period", node_id)),
                "operator": _operator_for(condition, node_id),
                "value": _num(threshold, "threshold", node_id),
            },
        }

    if indicator == "EMA":
        fast = block.get("fast_period")
        slow = block.get("slow_period")
        if fast is None or slow is None:
            raise LossyConversionError(
                f"block {node_id!r}: EMA block needs both 'fast_period' and 'slow_period'"
            )
        # ma_cross_condition is a CROSS comparison. Level words like "above" are
        # legal for RSI but have no meaning here, and emitting direction "gt"
        # would produce a config that parses and never fires -- the exact trap
        # this module exists to prevent. Cross words only.
        if condition not in _CROSS_RENAMES:
            raise LossyConversionError(
                f"block {node_id!r}: EMA needs a cross condition "
                f"('crosses_above'/'crosses_below'), got {condition!r}. "
                f"ma_cross_condition is a cross comparison; level words are not "
                f"expressible and will not evaluate."
            )
        return {
            "id": node_id,
            "type": "ma_cross_condition",
            "analysis_level": "minute_bar_filter",
            "params": {
                "fast_period": int(_num(fast, "fast_period", node_id)),
                "slow_period": int(_num(slow, "slow_period", node_id)),
                "direction": _CROSS_RENAMES[condition],
            },
        }

    if indicator == "BB":
        period = block.get("period")
        std_dev = block.get("std_dev")
        if period is None or std_dev is None:
            raise LossyConversionError(
                f"block {node_id!r}: BB block needs both 'period' and 'std_dev'"
            )
        if condition not in _BB_CHECK_TYPES:
            raise LossyConversionError(
                f"block {node_id!r}: Bollinger condition {condition!r} has no equivalent. "
                f"bollinger_bands_condition only supports "
                f"{sorted(set(_BB_CHECK_TYPES.values()))} "
                f"(condition_core.py:120). Substituting one would silently change "
                f"the strategy -- rebuild it in the visual editor instead."
            )
        return {
            "id": node_id,
            "type": "bollinger_bands_condition",
            "analysis_level": "minute_bar_filter",
            "params": {
                "period": int(_num(period, "period", node_id)),
                "std_dev": _num(std_dev, "std_dev", node_id),
                "check_type": _BB_CHECK_TYPES[condition],
            },
        }

    raise LossyConversionError(
        f"block {node_id!r}: unsupported indicator {block.get('indicator')!r}; "
        f"refusing to guess an equivalent"
    )


def convert_legacy_blocks_config(
    config: Dict[str, Any],
    *,
    sl_atr: float = 1.5,
    tp_rr: float = 2.0,
    direction: Optional[str] = None,
) -> Dict[str, Any]:
    """Legacy `blocks` config -> engine-readable config.

    The legacy format carries NO stop-loss and NO take-profit, so `sl_atr` and
    `tp_rr` are required decisions rather than translations. They default to the
    `rsi-breakout-v2` seed's values so a converted strategy behaves like the
    template it came from, but they are explicit parameters precisely because
    nothing in the source data implies them.

    `direction` is DERIVED from the entry blocks' `action` when they carry one,
    because a caller defaulting to "LONG" would otherwise silently turn an
    `open_short` strategy into a long one. Pass it explicitly only to assert a
    direction; a contradiction raises rather than being resolved silently.

    Raises `LossyConversionError` -- never returns a best-effort config.
    """
    if not isinstance(config, dict):
        raise LossyConversionError(f"config must be a dict, got {type(config).__name__}")

    blocks = config.get("blocks")
    if blocks is None:
        raise LossyConversionError("config has no 'blocks' key; nothing to convert")
    if not isinstance(blocks, list) or not blocks:
        raise LossyConversionError("'blocks' must be a non-empty list")

    # Two blocks sharing an id would produce a condition tree with ambiguous
    # nodes, and the engine resolves some lookups by id.
    seen_ids: set = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise LossyConversionError(f"block is not an object: {block!r}")
        bid = block.get("id")
        if bid is not None:
            if bid in seen_ids:
                raise LossyConversionError(
                    f"duplicate block id {bid!r}; ids must be unique within a config"
                )
            seen_ids.add(bid)

    entries: List[Dict[str, Any]] = []
    exits: List[Dict[str, Any]] = []
    open_actions: set = set()

    for block in blocks:
        action = block.get("action")
        node = _block_to_condition_node(block)
        if action in _EXIT_ACTIONS:
            exits.append(node)
        elif action in (None, "open_long", "open_short"):
            entries.append(node)
            if action is not None:
                open_actions.add(action)
        else:
            raise LossyConversionError(
                f"block {block.get('id')!r}: unsupported action {action!r}"
            )

    if not entries:
        raise LossyConversionError(
            "no entry block found; a converted config with only exits can never open a position"
        )

    if len(open_actions) > 1:
        raise LossyConversionError(
            f"entry blocks disagree about direction ({sorted(open_actions)}); "
            f"refusing to pick one"
        )
    derived = "SHORT" if "open_short" in open_actions else "LONG"
    if direction is not None and str(direction).upper() != derived:
        raise LossyConversionError(
            f"direction={direction!r} contradicts the entry blocks, which say {derived!r}"
        )
    direction = derived

    converted: Dict[str, Any] = {
        "timeframe": config.get("timeframe"),
        "symbol": config.get("symbol"),
        "filters": {"id": _uid("f"), "type": "AND", "children": []},
        "entryConditions": {
            "id": _uid("e"),
            "type": "AND",
            "children": entries,
        },
        "initialization": {
            "id": _uid("open"),
            "type": "open_position",
            "params": {
                "direction": direction,
                "risk_type": "percent_balance",
                "risk_value": 1.0,
                "sl_type": "atr_multiplier",
                "sl_value": _num(sl_atr, "sl_atr", "initialization"),
                "tp_type": "rr_multiplier",
                "tp_value": _num(tp_rr, "tp_rr", "initialization"),
            },
        },
    }

    if exits:
        # close_position is an ACTION, not a condition node (strategy.py:3685).
        # It only runs through a `conditional_management` wrapper that evaluates
        # `if_conditions` and then dispatches `then_actions` (strategy.py:4307).
        converted["positionManagement"] = [
            {
                "id": _uid("pm"),
                "type": "conditional_management",
                "if_conditions": {
                    "id": _uid("x"),
                    "type": "AND",
                    "children": exits,
                },
                "then_actions": [
                    {
                        "id": _uid("act"),
                        "type": "close_position",
                        "params": {},
                    }
                ],
            }
        ]

    return converted


def is_legacy_blocks_config(config: Any) -> bool:
    """True when a stored config still carries the unread `blocks` key."""
    return isinstance(config, dict) and "blocks" in config


def build_rsi_exit_block(
    period: int = 14,
    threshold: float = 75.0,
    operator: str = "cross_above",
) -> List[Dict[str, Any]]:
    """An RSI take-profit expressed as engine-format `positionManagement`.

    Needed for rows whose ORIGINAL `blocks` are already gone -- repaired by the
    template swap, which overwrote config_data and dropped the exit. Those rows
    cannot be recovered by `convert_legacy_blocks_config`; the exit has to be
    injected instead.

    Shape mirrors what the converter emits for a `close_position` block, so an
    injected exit and a converted one are indistinguishable to the engine.
    `close_position` is an ACTION and only runs inside `conditional_management`
    (strategy.py:4307).

    `analysis_level` is `minute_bar_filter` on purpose: a node is only skipped
    during a cheap scan when it says `second_bar_trigger` (strategy.py:5248-5254),
    so a take-profit must be evaluated at every level.
    """
    if operator not in ("cross_above", "cross_below"):
        raise LossyConversionError(
            f"exit operator {operator!r} is not a cross; refusing to build an exit"
        )
    return [
        {
            "id": _uid("pm"),
            "type": "conditional_management",
            "if_conditions": {
                "id": _uid("x"),
                "type": "AND",
                "children": [
                    {
                        "id": f"rsi_exit_{_uid('r')}",
                        "type": "rsi_condition",
                        "analysis_level": "minute_bar_filter",
                        "params": {
                            "period": int(period),
                            "operator": operator,
                            "value": _num(threshold, "threshold", "exit"),
                        },
                    }
                ],
            },
            "then_actions": [
                {"id": _uid("act"), "type": "close_position", "params": {}}
            ],
        }
    ]