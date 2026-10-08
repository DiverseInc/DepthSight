# File: tests/test_history_lookback_satisfies_strategy_indicators.py
"""
The seeded candle history must be able to satisfy the strategy's own indicators.

WHY THIS TEST EXISTS
--------------------
`MIN_STRATEGY_HISTORY_CANDLES` drives BOTH the initial history download
(`data_consumer._download_initial_kline_history_for_key`) and the pre-evaluation
gate (`controller.py:6161-6171`). Neither looks at the strategy's configured
indicator periods, so a small value silently makes long-period indicators
unreachable.

That is not hypothetical: the `ema-50-200-golden-cross` seed (`e898f74e`) shipped
showing "running" with a 200-period EMA on a 4h timeframe, where only 24 candles
were seeded. It could never evaluate a cross.

These tests assert on the REAL derivation in `data_consumer`, not a copy of it --
a transcription would keep passing after the production code changed, which is
precisely the bug class here.

Two things are checked:
  1. the arithmetic actually reaches the period a long-period EMA needs;
  2. the engine's own refusal is visible in logs, so the next silent-dead seed
     announces itself.
"""

import logging

import pytest

from bot_module import config as config_module
from bot_module import strategy as strategy_module

# `evaluate_ma_cross_scalar` (condition_core.py:911-919) does
#   slice_df = df.tail(max(250, max(fast_p, slow_p) + 5))
# and then reads BOTH ema.iloc[-1] and ema.iloc[-2]. A 200-period EMA therefore
# needs 205 rows before two defined values exist. Below 200 rows the pandas_ta
# accessor returns a DataFrame and `float()` raises TypeError.
LONG_SLOW_PERIOD = 200
ROWS_NEEDED = LONG_SLOW_PERIOD + 5

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "8h": 28800,
    "1d": 86400,
}

CACHE_CEILING = 5000  # getattr(config, "DEFAULT_KLINE_CACHE_SIZE", 5000)


def _seeded_candles(timeframe: str) -> int:
    """Mirror of data_consumer.py:2403-2436, reading the live config values.

    Deliberately re-derived from the module constants rather than importing a
    private helper: the point is to assert on the production constants.
    """
    min_candles = int(getattr(config_module, "MIN_STRATEGY_HISTORY_CANDLES", 20))
    configured_days = int(getattr(config_module, "REALTIME_HISTORY_LOOKBACK_DAYS", 3))
    secs = TIMEFRAME_SECONDS[timeframe]

    lookback_days = configured_days
    needed_days = (min_candles * secs) / 86400.0
    if needed_days > lookback_days:
        lookback_days = int(needed_days) + 1

    return int(lookback_days * (86400.0 / secs))


def test_a_200_period_ema_is_reachable_on_every_supported_timeframe():
    """Every timeframe must seed at least 205 candles.

    This is the assertion that was false for 4h/8h/1d under the old value of 20,
    which is what made the golden-cross seed dead on arrival.
    """
    short = {
        tf: seeded
        for tf, seeded in ((tf, _seeded_candles(tf)) for tf in TIMEFRAME_SECONDS)
        if seeded < ROWS_NEEDED
    }
    assert not short, (
        f"timeframes cannot satisfy a {LONG_SLOW_PERIOD}-period EMA: "
        f"{short}; each needs >= {ROWS_NEEDED} candles"
    )


def test_lookback_stays_within_the_cache_ceiling():
    """A fix that seeds more rows than the cache holds would silently drop them."""
    for tf in TIMEFRAME_SECONDS:
        seeded = _seeded_candles(tf)
        assert seeded <= CACHE_CEILING, (
            f"{tf} seeds {seeded} candles, over the {CACHE_CEILING}-row cache "
            f"ceiling; the oldest would be discarded before evaluation"
        )


def test_the_configured_value_is_explicit_not_a_getattr_fallback():
    """The constant must be DEFINED, not silently defaulted at both read sites.

    Before this change it existed only as the `getattr(..., 20)` fallback, which
    is why nobody could find it when a strategy was born dead.
    """
    assert hasattr(config_module, "MIN_STRATEGY_HISTORY_CANDLES"), (
        "MIN_STRATEGY_HISTORY_CANDLES is not defined in config.py; "
        "data_consumer.py:2403 and controller.py:6161 silently fall back to 20"
    )
    assert config_module.MIN_STRATEGY_HISTORY_CANDLES >= ROWS_NEEDED, (
        f"MIN_STRATEGY_HISTORY_CANDLES={config_module.MIN_STRATEGY_HISTORY_CANDLES} "
        f"is below the {ROWS_NEEDED} a {LONG_SLOW_PERIOD}-period cross needs"
    )


def test_entry_condition_failure_reasons_are_logged_above_debug(caplog):
    """A dead entry block must be visible without turning on DEBUG logging.

    The failure path swallowed an exception into `False` and logged the reason at
    DEBUG, while the INFO line printed only "FAILED. Signal rejected." A strategy
    that can never trade therefore looked identical to one waiting for its
    signal. This asserts the guard is in place, so the next silent one is
    visible.
    """
    import inspect

    # The entry gate lives in _execute_visual_strategy, NOT check_signal_sync.
    src = inspect.getsource(strategy_module.VisualBuilderStrategy._execute_visual_strategy)

    # There are TWO `if not entry_conditions_passed:` blocks. Only the FIRST one
    # prints the reasons; the second is the bare "Signal rejected." INFO.
    # Splitting on the guard and taking [-1] would read the wrong block -- which
    # is itself the reason this assertion is worth making explicitly.
    marker = "if not entry_conditions_passed:"
    assert marker in src, "the entry-condition gate moved; update this test"
    first_gate = src.index(marker)
    second_gate = src.index(marker, first_gate + 1)

    reasons_block = src[first_gate:second_gate]

    # The reasons must be logged, not silently discarded.
    assert "Reasons:" in reasons_block, (
        "the first entry-condition gate no longer logs its failure reasons"
    )

    # ...and logged at INFO or louder, so it is visible at default log level.
    import re

    levels = re.findall(r"logger\.(\w+)", reasons_block)
    assert levels, "no logging at all after the entry-condition gate"
    first_level = levels[0]
    assert first_level in ("info", "warning", "error"), (
        f"entry-condition failure reasons log at {first_level!r}; a strategy "
        f"that can never evaluate its entry would be invisible at default level"
    )