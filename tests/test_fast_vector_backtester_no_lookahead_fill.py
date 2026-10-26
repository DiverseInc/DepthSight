# tests/test_fast_vector_backtester_no_lookahead_fill.py
"""The backtester must never fill a gap with a FUTURE observation.

`pd.Series.bfill()` copies the NEXT valid value backwards in time. In a
backtester that means a higher-timeframe candle (or an indicator warm-up
value) that had not closed yet is injected into earlier rows, so the backtest
reports a result live trading cannot reproduce.

These tests defend the causal fill direction:
  * an indicator warm-up region stays NaN instead of being back-filled;
  * an indicator's value on a row never depends on data after that row;
  * a higher-timeframe indicator broadcast to 1m equals the causal broadcast;
  * an externally supplied ATR is not back-filled from a later bar;
  * the backtester source contains no `.bfill()` call at all.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot_module.fast_vector_backtester import FastVectorBacktester

pd_ta = pytest.importorskip("pandas_ta")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _klines(periods: int = 240, price_offset: float = 0.0) -> pd.DataFrame:
    """Deterministic OHLCV series (no RNG, no wall clock)."""
    index = pd.date_range("2024-01-01 00:00:00", periods=periods, freq="1min")
    close = (
        100.0
        + np.linspace(0.0, 12.0, periods)
        + np.sin(np.arange(periods) / 6.0) * 2.5
        + price_offset
    )
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    return pd.DataFrame(
        {
            "open": open_,
            "high": close + 0.6,
            "low": close - 0.6,
            "close": close,
            "volume": np.linspace(100.0, 200.0, periods),
        },
        index=index,
    )


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (
        df.resample(rule, label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
    )


def _ema_strategy(period: int, timeframe: str) -> dict:
    """A strategy that requests EMA_{period} on `timeframe` as an indicator."""
    return {
        "id": "no-lookahead-strategy",
        "name": "No Lookahead Strategy",
        "symbol": "TESTUSDT",
        "marketType": "FUTURES",
        "min_foundation_weight_threshold": 0,
        "filters": {"id": "f_root", "type": "AND", "children": []},
        "entryTrigger": {"type": "on_candle_close", "timeframe": "1m"},
        "entryConditions": {
            "id": "e_root",
            "type": "AND",
            "children": [
                {
                    "id": "ema_gt_close",
                    "type": "value_comparison",
                    "params": {
                        "leftOperand": {
                            "source": "indicator",
                            "key": f"EMA_{period}",
                            "timeframe": timeframe,
                        },
                        "operator": "gt",
                        "rightOperand": {"source": "candle", "key": "close"},
                    },
                }
            ],
        },
        "initialization": {
            "id": "init1",
            "type": "open_position",
            "params": {
                "direction": "LONG",
                "sl_type": "percent",
                "sl_value": 1.5,
                "tp_type": "percent",
                "tp_value": 3.0,
            },
        },
        "positionManagement": [],
    }


def _prepared_signals(klines, strategy) -> pd.DataFrame:
    bt = FastVectorBacktester(klines, strategy)
    bt._prepare_data()
    return bt.signals


def test_indicator_warmup_is_not_backfilled_with_a_future_value():
    """EMA(20) cannot be known for its first 19 bars; those bars stay NaN.

    With `.bfill()` the warm-up region is replaced by the first computed EMA
    value, i.e. a value that only exists 19 bars later.
    """
    signals = _prepared_signals(_klines(240), _ema_strategy(20, "1m"))

    assert "EMA_20" in signals.columns
    warmup = signals["EMA_20"].iloc[:19]
    assert warmup.isna().all(), (
        "EMA warm-up region must stay NaN; a non-NaN value there means a "
        "future observation was back-filled into earlier rows"
    )


def test_indicator_equals_the_causal_computation():
    """The indicator must be exactly what a causal calculation produces."""
    df = _klines(240)
    signals = _prepared_signals(df, _ema_strategy(20, "1m"))

    expected = pd_ta.ema(close=df["close"], length=20)
    pd.testing.assert_series_equal(
        signals["EMA_20"].reindex(df.index),
        expected,
        check_names=False,
    )


def test_indicator_value_does_not_depend_on_rows_after_it():
    """Causality: changing later rows must not change an earlier value.

    Rows 10..239 are replaced with a different price path. The EMA values on
    rows 0..9 must not move. With `.bfill()` row 0..9 carry the EMA computed
    at row 19, so they DO move - that is the look-ahead.
    """
    prefix_len = 10
    base = _klines(240)
    altered = base.copy()
    altered.iloc[prefix_len:, altered.columns.get_loc("close")] += 250.0
    altered.iloc[prefix_len:, altered.columns.get_loc("open")] += 250.0
    altered.iloc[prefix_len:, altered.columns.get_loc("high")] += 250.0
    altered.iloc[prefix_len:, altered.columns.get_loc("low")] += 250.0

    strategy = _ema_strategy(20, "1m")
    base_signals = _prepared_signals(base, strategy)
    altered_signals = _prepared_signals(altered, strategy)

    pd.testing.assert_series_equal(
        base_signals["EMA_20"].iloc[:prefix_len],
        altered_signals["EMA_20"].iloc[:prefix_len],
        check_names=False,
    )


def test_higher_timeframe_indicator_broadcast_equals_the_causal_broadcast():
    """A 15m indicator may only reach 1m after its 15m bar has closed."""
    df_1m = _klines(240)
    df_15m = _resample(df_1m, "15min")
    period = 3

    signals = _prepared_signals(
        {"1m": df_1m, "15m": df_15m}, _ema_strategy(period, "15m")
    )
    assert "EMA_3" in signals.columns

    ema_15m = pd_ta.ema(close=df_15m["close"], length=period)
    causal = ema_15m.shift(1).reindex(df_1m.index, method="ffill")

    pd.testing.assert_series_equal(
        signals["EMA_3"].reindex(df_1m.index),
        causal,
        check_names=False,
    )


def test_external_atr_column_is_not_backfilled_with_a_future_value():
    """An externally supplied ATR_14 must not borrow a later bar's value."""
    df = _klines(240)
    atr = pd_ta.atr(high=df["high"], low=df["low"], close=df["close"], length=14)
    # An external source typically has no ATR for its first bars.
    df["ATR_14"] = atr
    assert df["ATR_14"].iloc[:13].isna().all()
    assert pd.notna(df["ATR_14"].iloc[13])

    bt = FastVectorBacktester(df, _ema_strategy(20, "1m"))
    bt._prepare_data()

    warmup = bt.main_df["ATR_14"].iloc[:13]
    assert (warmup == 0.0).all(), (
        "an unknown ATR must fall back to a neutral value, not to a value "
        "computed from bars that had not happened yet"
    )


def _kitchen_sink_strategy(timeframe: str) -> dict:
    """A strategy that requests every indicator family the engine can build.

    `bollinger_bands_condition` -> BB_*, `stochastic_condition` -> STOCH_*,
    `macd_condition` -> MACD_*, `adx_filter` -> ADX_14, `natr_filter` ->
    NATR_14, `rsi_condition` -> RSI_14, `trend_direction` -> SMA_* / RSI_*,
    `ma_cross_condition` -> EMA_*.
    """
    return {
        "id": "no-lookahead-kitchen-sink",
        "name": "No Lookahead Kitchen Sink",
        "symbol": "TESTUSDT",
        "marketType": "FUTURES",
        "min_foundation_weight_threshold": 0,
        "filters": {"id": "f_root", "type": "AND", "children": []},
        "entryTrigger": {"type": "on_candle_close", "timeframe": "1m"},
        "entryConditions": {
            "id": "e_root",
            "type": "AND",
            "children": [
                {
                    "id": "ema",
                    "type": "ma_cross_condition",
                    "params": {
                        "fast_period": 9,
                        "slow_period": 21,
                        "direction": "long",
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "rsi",
                    "type": "rsi_condition",
                    "params": {
                        "period": 14,
                        "operator": "gt",
                        "value": 50,
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "natr",
                    "type": "natr_filter",
                    "params": {
                        "period": 14,
                        "operator": "gt",
                        "value": 0.0,
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "adx",
                    "type": "adx_filter",
                    "params": {
                        "period": 14,
                        "operator": "gt",
                        "value": 0.0,
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "macd",
                    "type": "macd_condition",
                    "params": {
                        "fast_period": 12,
                        "slow_period": 26,
                        "signal_period": 9,
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "bb",
                    "type": "bollinger_bands_condition",
                    "params": {
                        "period": 20,
                        "std_dev": 2.0,
                        "check_type": "cross_above",
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "stoch",
                    "type": "stochastic_condition",
                    "params": {
                        "k_period": 14,
                        "d_period": 3,
                        "smooth_k": 3,
                        "timeframe": timeframe,
                    },
                },
                {
                    "id": "trend",
                    "type": "trend_direction",
                    "params": {
                        "sma_fast_period": 9,
                        "sma_slow_period": 21,
                        "rsi_period": 14,
                        "direction": "long",
                        "timeframe": timeframe,
                    },
                },
            ],
        },
        "initialization": {
            "id": "init1",
            "type": "open_position",
            "params": {
                "direction": "LONG",
                "sl_type": "percent",
                "sl_value": 1.5,
                "tp_type": "percent",
                "tp_value": 3.0,
            },
        },
        "positionManagement": [],
    }


def test_no_signal_column_depends_on_rows_after_it():
    """Generic causality invariant across every indicator family.

    Rows 10..239 are replaced with a different price path. Every signal the
    engine computed must be identical on rows 0..9, because a value there may
    only depend on the bars up to that row. With `.bfill()` the warm-up
    region of each indicator is filled with a value computed further down the
    series, so it moves when the later rows move.
    """
    prefix_len = 10
    base = _klines(240)
    altered = base.copy()
    for col in ("close", "open", "high", "low"):
        altered.iloc[prefix_len:, altered.columns.get_loc(col)] += 250.0

    base_signals = _prepared_signals(base, _kitchen_sink_strategy("1m"))
    altered_signals = _prepared_signals(altered, _kitchen_sink_strategy("1m"))

    assert len(base_signals.columns) > 1
    for col in base_signals.columns:
        pd.testing.assert_series_equal(
            base_signals[col].iloc[:prefix_len],
            altered_signals[col].iloc[:prefix_len],
            check_names=False,
            obj=f"signal column {col!r} changed when only later rows changed",
        )


def test_no_mtf_signal_column_depends_on_rows_after_it():
    """The same causality invariant for a higher-timeframe broadcast.

    A 5m indicator broadcast to 1m must not carry a value computed from 5m
    bars that had not closed yet.
    """
    prefix_len = 10
    base = _klines(240)
    altered = base.copy()
    for col in ("close", "open", "high", "low"):
        altered.iloc[prefix_len:, altered.columns.get_loc(col)] += 250.0

    def signals_for(df_1m):
        return _prepared_signals(
            {"1m": df_1m, "5m": _resample(df_1m, "5min")},
            _kitchen_sink_strategy("5m"),
        )

    base_signals = signals_for(base)
    altered_signals = signals_for(altered)

    assert len(base_signals.columns) > 1
    for col in base_signals.columns:
        pd.testing.assert_series_equal(
            base_signals[col].iloc[:prefix_len],
            altered_signals[col].iloc[:prefix_len],
            check_names=False,
            obj=f"signal column {col!r} changed when only later rows changed",
        )


def test_main_atr_series_does_not_borrow_a_future_atr_value():
    """`_get_main_atr_series()` must not back-fill an unknown ATR.

    The method is also called before `_prepare_data()` has normalised
    `main_df["ATR_14"]`, so it has to be safe on its own.
    """
    df = _klines(240)
    atr = pd_ta.atr(high=df["high"], low=df["low"], close=df["close"], length=14)
    df["ATR_14"] = atr
    assert df["ATR_14"].iloc[:13].isna().all()

    bt = FastVectorBacktester(df, _ema_strategy(20, "1m"))
    series = bt._get_main_atr_series()

    assert series.iloc[:13].isna().all(), (
        "an unknown ATR must not be filled with a value computed from a later bar"
    )


def test_backtester_never_fills_a_gap_from_the_future():
    """Static guard: `.bfill()` must not reappear in the backtester."""
    source = (REPO_ROOT / "bot_module" / "fast_vector_backtester.py").read_text(
        encoding="utf-8"
    )
    assert ".bfill(" not in source, (
        "bot_module/fast_vector_backtester.py must not use .bfill(): it fills "
        "gaps with the NEXT (future) observation and creates look-ahead bias"
    )


@pytest.mark.parametrize(
    "node_type,params,warmup_minutes",
    [
        (
            "volatility_filter",
            {
                "indicator": "ATR",
                "period": 14,
                "operator": "gt",
                "value": 0.0,
                "timeframe": "5m",
            },
            # ATR(14) needs 14 x 5m bars; the value reaches 1m at minute 70.
            70,
        ),
        (
            "volatility_filter",
            {
                "indicator": "BBW",
                "period": 20,
                "operator": "gt",
                "value": 0.0,
                "timeframe": "5m",
            },
            # BB(20) needs 20 x 5m bars; the value reaches 1m at minute 100.
            100,
        ),
        (
            "natr_filter",
            {
                "period": 14,
                "operator": "gt",
                "value": 0.0,
                "timeframe": "5m",
            },
            # NATR(14) needs 14 x 5m bars; the value reaches 1m at minute 70.
            70,
        ),
    ],
)
def test_higher_timeframe_volatility_filter_stays_neutral_before_it_exists(
    node_type, params, warmup_minutes
):
    """A 5m volatility filter must be neutral until the 5m indicator exists.

    The 5m ATR/BBW/NATR cannot be known before their warm-up completes. With
    `.bfill()` the filter would be handed a value computed from 5m bars that
    had not closed yet, and it would start admitting trades immediately.
    """
    df_1m = _klines(240)
    df_5m = _resample(df_1m, "5min")

    bt = FastVectorBacktester({"1m": df_1m, "5m": df_5m}, _ema_strategy(20, "1m"))
    bt._prepare_data()

    if node_type == "natr_filter":
        mask = bt._evaluate_natr_filter(params)
    else:
        mask = bt._evaluate_volatility_filter(params)

    mask = mask.reindex(bt.main_df.index).fillna(False).astype(bool)

    warmup_window = bt.main_df.index[:warmup_minutes]
    assert not mask.reindex(warmup_window).any(), (
        "the filter must stay neutral until the higher-timeframe indicator "
        "actually exists; a True value here means a future bar was back-filled"
    )
    # Guard against a filter that is simply never true.
    assert mask.reindex(bt.main_df.index[warmup_minutes:]).any(), (
        "the filter must start firing once the indicator exists"
    )


def test_utils_fill_comments_match_pandas_semantics():
    """The `.bfill()`/`.ffill()` labels in bot_module/utils.py must be correct.

    Verified empirically against pandas:
        bfill() -> fills from the NEXT valid value   (backward fill)
        ffill() -> fills from the PREVIOUS valid value (forward fill)
    """
    source = (REPO_ROOT / "bot_module" / "utils.py").read_text(encoding="utf-8")

    bfill_lines = [
        line.strip()
        for line in source.splitlines()
        if ".bfill()" in line and "#" in line
    ]
    ffill_lines = [
        line.strip()
        for line in source.splitlines()
        if ".ffill()" in line and "#" in line
    ]

    assert bfill_lines, "expected a commented .bfill() call in bot_module/utils.py"
    assert ffill_lines, "expected a commented .ffill() call in bot_module/utils.py"

    for line in bfill_lines:
        comment = line.split("#", 1)[1].lower()
        assert "backward" in comment, f"'.bfill()' is a BACKWARD fill: {line!r}"
        assert "forward fill" not in comment, (
            f"'.bfill()' is a BACKWARD fill, not a forward fill: {line!r}"
        )

    for line in ffill_lines:
        comment = line.split("#", 1)[1].lower()
        assert "forward" in comment, f"'.ffill()' is a FORWARD fill: {line!r}"
        assert "backward fill" not in comment, (
            f"'.ffill()' is a FORWARD fill, not a backward fill: {line!r}"
        )


def test_pandas_fill_direction_semantics_are_what_this_suite_assumes():
    """Pin the pandas behaviour the whole suite depends on."""
    tagged = pd.Series([np.nan, 11.0, np.nan, 13.0], dtype="float64")

    backfilled = tagged.bfill()
    assert backfilled.iloc[0] == 11.0  # copied from the LATER row -> future
    assert backfilled.iloc[2] == 13.0

    forward_filled = tagged.ffill()
    assert pd.isna(forward_filled.iloc[0])  # nothing in the past to copy
    assert forward_filled.iloc[2] == 11.0  # copied from the EARLIER row -> past
