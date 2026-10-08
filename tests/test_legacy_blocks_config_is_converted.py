# File: tests/test_legacy_blocks_config_is_converted.py
"""
The legacy `blocks` strategy format must be translatable into a config the
engine actually evaluates -- or refused loudly.

WHY THIS TEST SHAPE
-------------------
A converted config that is well-formed but never fires is the SAME failure that
shipped the dead seeds: the dashboard says "running", nothing trades, forever.
Shape assertions cannot catch that, so these tests drive the REAL
`VisualBuilderStrategy.check_signal_sync` and require an actual `StrategySignal`
out of the converted config.

The force mechanism matters and is easy to get wrong: the engine reads the
CURRENT indicator value from `pair_info` and the PREVIOUS one from the candle
frame at `current_candle_index - 1` (`_get_previous_indicator_value`). A fixture
that sets only the last row leaves prev=None and every cross silently evaluates
False -- which would make this file pass for the wrong reason, or fail for the
wrong one. Both rows are set explicitly below.

The legacy rows themselves are transcribed verbatim from the production
database (ids elided) so the mapping is exercised against the real shapes,
including the one that is genuinely lossy.
"""

import numpy as np
import pandas as pd
import pytest

from bot_module import strategy as strategy_module
from bot_module.legacy_blocks_migration import (
    LossyConversionError,
    convert_legacy_blocks_config,
    is_legacy_blocks_config,
)
from bot_module.strategy import SignalDirection, StrategySignal, VisualBuilderStrategy


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_visual(monkeypatch):
    monkeypatch.setitem(
        strategy_module.STRATEGIES, "VisualBuilderStrategy", VisualBuilderStrategy
    )
    monkeypatch.setattr(
        strategy_module.config, "MIN_TOTAL_FOUNDATION_WEIGHT_THRESHOLD", 0.0
    )


def _klines(n=300, base=100.0, seed=11):
    rng = np.random.default_rng(seed)
    now = pd.Timestamp("2024-01-10 12:00:00", tz="UTC")
    index = pd.to_datetime(
        [now - pd.Timedelta(minutes=i) for i in range(n - 1, -1, -1)]
    )
    return pd.DataFrame(
        {
            "open": rng.uniform(base - 1, base, n),
            "high": rng.uniform(base, base + 1, n),
            "low": rng.uniform(base - 2, base - 1, n),
            "close": rng.uniform(base - 0.5, base + 0.5, n),
            "volume": rng.uniform(100, 200, n),
        },
        index=index,
    )


def _market_data():
    df = _klines()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    trades = pd.DataFrame(
        {"price": np.full(100, 100.0), "quantity": np.full(100, 1.0)},
        index=pd.date_range(end=df.index[-1], periods=100, freq="500ms", tz="UTC"),
    )
    return {
        "kline_1m": df.copy(),
        "kline_5m": df.resample("5min").agg(agg).dropna(),
        "kline_15m": df.resample("15min").agg(agg).dropna(),
        "kline_1h": df.resample("1h").agg(agg).dropna(),
        "depth_trading": {"bids": [], "asks": []},
        "aggTrade": trades,
    }


def _pair_info(**over):
    info = {
        "symbol": "BTCUSDT",
        "natr": 2.0,
        "relative_volume": 3.0,
        "atr": 1.0,
        "tick_size": 0.01,
        "last_price": 100.0,
        "open": 99.8,
        "high": 100.3,
        "low": 99.6,
        "close": 100.0,
        "current_candle_index": 59,
        "candle_timeframe": "1m",
        "RSI_14": 50,
        "ADX_14": 20.0,
        "BBL_20_2.0": 95.0,
        "BBU_20_2.0": 105.0,
        "BBB_20_2.0": 10.0,
    }
    info.update(over)
    return info


def _instance(cfg):
    inst = strategy_module.create_strategy_instance(
        strategy_name="VisualBuilderStrategy",
        params={"config": dict(cfg), "enabled": True},
    )
    assert inst is not None
    return inst


# --- the real legacy rows (transcribed from the production database) ---------

RSI_LEGACY = {
    "timeframe": "1h",
    "symbol": "BTCUSDT",
    "blocks": [
        {
            "id": "rsi_entry",
            "type": "indicator",
            "indicator": "RSI",
            "period": 14,
            "condition": "crosses_above",
            "threshold": 55,
        },
        {
            "id": "rsi_exit",
            "type": "indicator",
            "indicator": "RSI",
            "period": 14,
            "condition": "crosses_above",
            "threshold": 75,
            "action": "close_position",
        },
    ],
}

BB_LEGACY = {
    "timeframe": "15m",
    "symbol": "ETHUSDT",
    "blocks": [
        {
            "id": "bb_entry",
            "type": "indicator",
            "indicator": "BB",
            "period": 20,
            "std_dev": 2.0,
            "condition": "touches_lower",
            "action": "open_long",
        },
        {
            "id": "bb_exit",
            "type": "indicator",
            "indicator": "BB",
            "period": 20,
            "std_dev": 2.0,
            "condition": "reaches_middle",
            "action": "close_position",
        },
    ],
}


# --- the rename traps -------------------------------------------------------


def test_legacy_condition_word_is_renamed_not_copied():
    """`crosses_above` must become `cross_above`.

    Copying the legacy string verbatim produces a config that parses fine and
    never fires -- the exact failure that shipped the dead seeds.
    """
    out = convert_legacy_blocks_config(RSI_LEGACY)
    node = out["entryConditions"]["children"][0]

    assert node["params"]["operator"] == "cross_above", (
        f"operator was {node['params']['operator']!r}; the engine dispatches on "
        f"'cross_above' (api/crud.py:4266) and would silently never evaluate"
    )
    assert "crosses_above" not in str(out), "legacy condition word leaked through"


def test_bollinger_touches_lower_becomes_price_below_lower():
    """`touches_lower` -> `price_below_lower` (condition_core.py:120)."""
    legacy = {
        "timeframe": "15m",
        "symbol": "ETHUSDT",
        "blocks": [
            {
                "id": "bb_entry",
                "indicator": "BB",
                "period": 20,
                "std_dev": 2.0,
                "condition": "touches_lower",
                "action": "open_long",
            }
        ],
    }
    node = convert_legacy_blocks_config(legacy)["entryConditions"]["children"][0]
    assert node["params"]["check_type"] == "price_below_lower"


def test_exit_becomes_a_conditional_management_block_not_an_entry():
    """`close_position` is an action and only runs via `conditional_management`."""
    out = convert_legacy_blocks_config(RSI_LEGACY)

    assert "positionManagement" in out, "the RSI-75 exit was dropped"
    pm = out["positionManagement"][0]
    assert pm["type"] == "conditional_management"  # strategy.py:4307
    assert pm["then_actions"][0]["type"] == "close_position"  # strategy.py:3685

    # and it must NOT have leaked into the entry gate
    entry_ids = [c["id"] for c in out["entryConditions"]["children"]]
    assert entry_ids == ["rsi_entry"], f"exit leaked into entryConditions: {entry_ids}"


# --- lossless refusal -------------------------------------------------------


def test_lossy_bollinger_exit_is_refused_not_approximated():
    """`reaches_middle` has no equivalent and must raise.

    The BB checker has exactly four check types and none of them means "price
    returned to the middle band". Substituting `price_above_upper` would produce
    a strategy that looks migrated and trades differently.
    """
    with pytest.raises(LossyConversionError) as exc:
        convert_legacy_blocks_config(BB_LEGACY)

    msg = str(exc.value)
    assert "reaches_middle" in msg
    assert "price_below_lower" in msg  # the tool tells you what IS supported


def test_unknown_indicator_is_refused():
    with pytest.raises(LossyConversionError):
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {
                        "id": "x",
                        "indicator": "ICHIMOKU",
                        "condition": "crosses_above",
                        "action": "open_long",
                    }
                ],
            }
        )


def test_unknown_rsi_condition_is_refused():
    with pytest.raises(LossyConversionError):
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {
                        "id": "x",
                        "indicator": "RSI",
                        "period": 14,
                        "condition": "is_bullish_divergence",
                        "threshold": 55,
                        "action": "open_long",
                    }
                ],
            }
        )


def test_config_without_blocks_is_refused():
    with pytest.raises(LossyConversionError):
        convert_legacy_blocks_config({"timeframe": "1h", "symbol": "BTCUSDT"})


def test_exits_only_config_is_refused():
    """An entry-less config can never open a position."""
    with pytest.raises(LossyConversionError):
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {
                        "id": "x",
                        "indicator": "RSI",
                        "period": 14,
                        "condition": "crosses_above",
                        "threshold": 75,
                        "action": "close_position",
                    }
                ],
            }
        )


def test_lossy_error_is_a_valueerror():
    """Config-parsing handlers catch ValueError; stay compatible with them."""
    assert issubclass(LossyConversionError, ValueError)


# --- OpenCode findings 2026-10-08: four defects, fixed and pinned here ------


def test_direction_is_derived_from_open_short_not_defaulted_to_long():
    """An `open_short` entry must produce a SHORT initialization, not a LONG one.

    The default was previously "LONG", which would have silently converted a
    short strategy into a long one -- a config that trades, trades the WRONG way,
    and reports nothing.
    """
    legacy = {
        "timeframe": "1h",
        "blocks": [
            {
                "id": "e",
                "indicator": "RSI",
                "period": 14,
                "condition": "crosses_above",
                "threshold": 30,
                "action": "open_short",
            }
        ],
    }
    out = convert_legacy_blocks_config(legacy)
    assert out["initialization"]["params"]["direction"] == "SHORT"


def test_contradictory_direction_is_refused():
    with pytest.raises(LossyConversionError) as exc:
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {
                        "id": "e",
                        "indicator": "RSI",
                        "period": 14,
                        "condition": "crosses_above",
                        "threshold": 30,
                        "action": "open_short",
                    }
                ],
            },
            direction="LONG",
        )
    assert "contradicts" in str(exc.value)


def test_mixed_direction_entries_are_refused():
    with pytest.raises(LossyConversionError) as exc:
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {
                        "id": "a",
                        "indicator": "RSI",
                        "period": 14,
                        "condition": "crosses_above",
                        "threshold": 30,
                        "action": "open_long",
                    },
                    {
                        "id": "b",
                        "indicator": "RSI",
                        "period": 14,
                        "condition": "crosses_below",
                        "threshold": 70,
                        "action": "open_short",
                    },
                ],
            }
        )
    assert "disagree" in str(exc.value)


def test_ema_level_condition_is_refused():
    """`ma_cross_condition` is a CROSS comparison; "above" has no meaning there.

    Emitting `direction: "gt"` would produce a config that parses and never
    fires -- the exact trap this module exists to prevent.
    """
    with pytest.raises(LossyConversionError) as exc:
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {
                        "id": "gc",
                        "indicator": "EMA",
                        "fast_period": 50,
                        "slow_period": 200,
                        "condition": "above",
                        "action": "open_long",
                    }
                ],
            }
        )
    assert "cross" in str(exc.value).lower()


def test_duplicate_block_ids_are_refused():
    legacy = {
        "timeframe": "1h",
        "blocks": [
            {"id": "dup", "indicator": "RSI", "period": 14,
             "condition": "crosses_above", "threshold": 55, "action": "open_long"},
            {"id": "dup", "indicator": "RSI", "period": 14,
             "condition": "crosses_below", "threshold": 30},
        ],
    }
    with pytest.raises(LossyConversionError) as exc:
        convert_legacy_blocks_config(legacy)
    assert "duplicate" in str(exc.value)


def test_generated_ids_never_collide_with_user_block_ids():
    """A user block named `open_position` must not collide with a generated id.

    Asserts uniqueness rather than a fixed count: the property that matters is
    that no id appears twice, since the engine resolves some lookups by id.
    """
    legacy = {
        "timeframe": "1h",
        "blocks": [
            {"id": "open_position", "indicator": "RSI", "period": 14,
             "condition": "crosses_above", "threshold": 55, "action": "open_long"},
            {"id": "e_root", "indicator": "RSI", "period": 14,
             "condition": "crosses_below", "threshold": 30, "action": "close_position"},
        ],
    }
    out = convert_legacy_blocks_config(legacy)

    ids: list = []

    def collect(node):
        if isinstance(node, dict):
            if node.get("id"):
                ids.append(node["id"])
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(out)

    # The user blocks deliberately reuse names the converter used to hardcode.
    assert "open_position" in ids and "e_root" in ids, (
        "fixture drifted -- it must use the colliding ids to be meaningful"
    )
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"generated ids collided with block ids: {duplicates}"
    assert len(ids) == len(set(ids)), f"duplicate ids emitted: {ids}"


def test_non_numeric_period_raises_lossy_not_typeerror():
    """`int([])` raises TypeError, which escapes every `except ValueError`."""
    with pytest.raises(LossyConversionError) as exc:
        convert_legacy_blocks_config(
            {
                "timeframe": "1h",
                "blocks": [
                    {"id": "x", "indicator": "RSI", "period": [],
                     "condition": "crosses_above", "threshold": 55, "action": "open_long"},
                ],
            }
        )
    assert "numeric" in str(exc.value)


# --- the thing that actually matters: drive the REAL engine ------------------


def test_converted_rsi_config_produces_a_real_signal():
    """Acceptance test: the converted config must actually trade.

    RSI_14 is set to 80 (above the 55 entry threshold) and the PREVIOUS candle
    row to 50 (below it), so `cross_above` genuinely crosses rather than
    trivially comparing against a stale value.
    """
    cfg = convert_legacy_blocks_config(RSI_LEGACY)
    inst = _instance(cfg)

    md = _market_data()
    pi = _pair_info(RSI_14=80.0)
    df = md["kline_1m"]
    cur = pi["current_candle_index"]
    df["RSI_14"] = 80.0
    df.iloc[cur - 1, df.columns.get_loc("RSI_14")] = 50.0
    df.iloc[cur, df.columns.get_loc("RSI_14")] = 80.0

    signal, weight, trace = inst.check_signal_sync(pi, md, None)

    assert isinstance(signal, StrategySignal), (
        f"converted config produced no signal; it would never trade. trace={trace}"
    )
    assert signal.direction == SignalDirection.LONG
    assert signal.stop_loss is not None and signal.stop_loss > 0
    assert signal.take_profit is not None and signal.take_profit > 0


def test_converted_config_still_declares_no_blocks_key():
    """The whole point: the engine can read it."""
    out = convert_legacy_blocks_config(RSI_LEGACY)
    assert "blocks" not in out
    assert is_legacy_blocks_config(out) is False
    assert is_legacy_blocks_config(RSI_LEGACY) is True


def test_sl_tp_come_from_the_caller_not_the_legacy_data():
    """The legacy format specifies neither, so they must be explicit decisions."""
    out = convert_legacy_blocks_config(RSI_LEGACY, sl_atr=3.0, tp_rr=1.5)
    params = out["initialization"]["params"]
    assert params["sl_value"] == 3.0
    assert params["tp_value"] == 1.5


def test_timeframe_and_symbol_are_preserved():
    out = convert_legacy_blocks_config(RSI_LEGACY)
    assert out["timeframe"] == "1h"
    assert out["symbol"] == "BTCUSDT"


def test_every_emitted_leaf_type_has_a_registered_checker():
    """A node type the dispatcher does not know would never evaluate.

    `condition_checkers` is an INSTANCE attribute built in `BaseStrategy.__init__`
    (`self.condition_checkers = {...}`, strategy.py:2148), not a class attribute,
    so the lookup has to go through a real instance.
    """
    out = convert_legacy_blocks_config(RSI_LEGACY)
    inst = _instance(out)
    roots = [out["entryConditions"]] + [
        pm["if_conditions"] for pm in out["positionManagement"]
    ]

    def leaves(node):
        if node.get("type") in ("AND", "OR"):
            for child in node.get("children") or []:
                yield from leaves(child)
        else:
            yield node

    found = [n["type"] for root in roots for n in leaves(root)]
    assert found, "converted config produced no condition leaves"
    for node_type in found:
        assert node_type in inst.condition_checkers, (
            f"{node_type!r} is not a registered checker (strategy.py:2148); "
            f"it would silently never evaluate"
        )