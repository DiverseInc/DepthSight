import numpy as np
import pandas as pd
import pytest

if not hasattr(np, "NaN"):
    np.NaN = np.nan

from bot_module.fast_vector_backtester import FastVectorBacktester
from bot_module.strategy import VisualBuilderStrategy


FAST_SLIPPAGE_PCT = 0.0
FAST_COMMISSION_PCT = 0.0


def _build_rtl_dataset(rows: int = 200, logic_type: str = "touch") -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq="1min", tz="UTC")

    open_vals = np.full(rows, 100.0)
    high_vals = np.full(rows, 100.5)
    low_vals = np.full(rows, 99.5)
    close_vals = np.full(rows, 100.0)

    if logic_type == "touch":
        # Level will be 100.0
        #
        # The warm-up price MUST sit strictly OUTSIDE the proximity band, not on
        # its boundary. Proximity defaults to 0.1 x ATR (see strategy.py:6771),
        # and ATR_14 is 5.0 below, so the band is 0.5. The old fixture parked
        # these bars at 100.5 -- distance exactly 0.5, i.e. ON the inclusive
        # boundary at strategy.py:6786 (`abs(last_price - level_price) <=
        # proximity`). Both engines then matched the warm-up bars as "near
        # level", which made this test unable to distinguish a correct engine
        # from a broken one and produced the 1-vs-100 signal divergence.
        # 101.0 is distance 1.0 -- comfortably outside the 0.5 band.
        open_vals[:50] = 100.5
        close_vals[:50] = 100.5
        high_vals[:50] = 100.6
        low_vals[:50] = 100.4

        # 50-100: price moves away to 110
        for i in range(50, 100):
            open_vals[i] = 101.0 + (i - 50) * 0.2
            close_vals[i] = 101.0 + (i - 49) * 0.2
            high_vals[i] = close_vals[i] + 0.1
            low_vals[i] = open_vals[i] - 0.1

        # 100-150: price stays at 110
        open_vals[100:150] = 110.0
        close_vals[100:150] = 110.0
        high_vals[100:150] = 110.5
        low_vals[100:150] = 109.5

        # 150: price returns to 100.1 (touching from above)
        open_vals[150] = 110.0
        close_vals[150] = 100.1
        high_vals[150] = 110.1
        low_vals[150] = 100.0

        # 151+: price departs IMMEDIATELY and stays away. Without this the tail
        # keeps the default fill (close = 100.0), which sits exactly ON the
        # level, so `price_is_near_level` stays true for the rest of the
        # series and the live engine re-fires on every remaining bar. That
        # left the touch at bar 150 unisolated for the `touch/any` case.
        for i in range(151, rows):
            v = 110.0 + (i - 151) * 0.1
            open_vals[i] = v
            close_vals[i] = v
            high_vals[i] = v + 0.1
            low_vals[i] = v - 0.1

    elif logic_type == "breakout_retest":
        # Level will be 100.0
        # 0-50: price at 100.1
        open_vals[:50] = 100.1
        close_vals[:50] = 100.1

        # 50-60: price breaks DOWN to 90
        for i in range(50, 60):
            open_vals[i] = 100.0 - (i - 50) * 1.0
            close_vals[i] = 100.0 - (i - 49) * 1.0
            high_vals[i] = open_vals[i] + 0.1
            low_vals[i] = close_vals[i] - 0.1

        # 60-150: price stays at 90
        open_vals[60:150] = 90.0
        close_vals[60:150] = 90.0
        high_vals[60:150] = 90.5
        low_vals[60:150] = 89.5

        # 150: price returns to 99.9 (retesting from below)
        open_vals[150] = 90.0
        close_vals[150] = 99.9
        high_vals[150] = 100.0
        low_vals[150] = 90.0

    df = pd.DataFrame(
        {
            "open": open_vals,
            "high": high_vals,
            "low": low_vals,
            "close": close_vals,
            "volume": 1000.0,
            "ATR_14": 5.0,  # Constant ATR for stable proximity zone (proximity will be 0.5)
            "NATR_14": 1.5,
            "natr": 1.5,
        },
        index=idx,
    )
    return df


def _build_rtl_strategy(retest_type: str = "touch", direction: str = "any") -> dict:
    return {
        "filters": {"id": "f1", "type": "AND", "children": []},
        "entryConditions": {
            "id": "entry_root",
            "type": "AND",
            "children": [
                {
                    "id": "rtl_block",
                    "type": "return_to_level",
                    "params": {
                        "level_source": {"source": "constant", "value": 100.0},
                        "retest_type": retest_type,
                        "approach_direction": direction,
                        "confirmation_time_sec": 0,
                    },
                }
            ],
        },
        "initialization": {
            "id": "init",
            "type": "open_position",
            "params": {
                "direction": "LONG",
                "sl_type": "percent_from_price",
                "sl_value": 5.0,
                "tp_type": "rr_multiplier",
                "tp_value": 2.0,
            },
        },
    }


def _to_utc_ts(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


async def _run_strategy_replay(
    df_live: pd.DataFrame, strategy_json: dict
) -> list[pd.Timestamp]:
    strategy = VisualBuilderStrategy(
        params={
            "config": strategy_json,
            "enabled": True,
            "min_total_foundation_weight_threshold": 0.0,
        }
    )
    market_data = {"kline_1m": df_live}

    signal_times: list[pd.Timestamp] = []
    prev_pair_info = None

    for i, (ts, row) in enumerate(df_live.iterrows()):
        pair_info = {
            **row.to_dict(),
            "symbol": "TESTUSDT",
            "last_price": float(row["close"]),
            "atr": float(row.get("ATR_14", 1.0)),  # BaseStrategy expects 'atr'
            "tick_size": 1e-8,
            "candle_timeframe": "1m",
            "timestamp_dt": ts.to_pydatetime(),
            "current_candle_index": i,
            "is_live_mode": False,
        }

        signal, _weight, _trace = await strategy.check_signal(
            pair_info,
            market_data,
            prev_pair_info,
            analysis_level="second_bar_trigger",
        )
        if signal is not None:
            signal_times.append(_to_utc_ts(ts))

        prev_pair_info = pair_info

    return signal_times


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "logic_type, approach_direction",
    [
        ("touch", "any"),
        ("touch", "from_above"),
        ("breakout_retest", "any"),
        ("breakout_retest", "from_below"),
    ],
)
async def test_return_to_level_parity(logic_type, approach_direction):
    strategy_json = _build_rtl_strategy(
        retest_type=logic_type, direction=approach_direction
    )
    df_live = _build_rtl_dataset(logic_type=logic_type)
    df_fast = df_live.copy()
    df_fast.index = df_fast.index.tz_convert(None)

    fast_bt = FastVectorBacktester(
        df_fast,
        strategy_json,
        execution_config={"commission_pct": 0.0, "slippage_pct": 0.0},
    )
    fast_bt.run()

    entry_conditions = strategy_json.get("entryConditions", {})
    mask, _ = fast_bt._evaluate_condition_tree_with_node_results(entry_conditions)

    strategy_signal_times = await _run_strategy_replay(df_live, strategy_json)
    fast_signal_times = [_to_utc_ts(ts) for ts in mask[mask].index]

    assert fast_signal_times == strategy_signal_times
