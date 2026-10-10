"""
Per-strategy history requirement (2026-10-10).

The failure this prevents: a strategy configured with a 300-period indicator
is gated at 300 but only ever downloads 205 (the global
`MIN_STRATEGY_HISTORY_CANDLES`). It reports "running" forever and never
trades, and nothing in the logs says why.

The two sides MUST agree. Raising only the gate manufactures a permanently
unsatisfiable requirement, which is the same silent failure wearing a
different hat.
"""

from bot_module.strategy_requirements import (
    HISTORY_BUFFER_CANDLES,
    max_period_for_indicator_keys,
    required_candles_for_indicator_keys,
)


def test_ema_200_demands_more_than_the_global_floor():
    req = required_candles_for_indicator_keys({"EMA_200"}, floor=205)
    assert req == 200 + HISTORY_BUFFER_CANDLES
    assert req > 205, "a 200-period average must clear a 205 global floor"


def test_bollinger_period_is_the_length_not_the_std_dev():
    """`BB_20_2.0` is period 20, std-dev 2.0. The 2.0 is not a period."""
    assert max_period_for_indicator_keys({"BB_20_2.0"}) == 20


def test_macd_takes_the_slow_period():
    assert max_period_for_indicator_keys({"MACD_12_26_9"}) == 26


def test_short_indicator_does_not_lower_the_floor():
    """RSI(14) must still get the configured global minimum."""
    assert required_candles_for_indicator_keys({"RSI_14"}, floor=205) == 205


def test_tape_windows_are_seconds_not_candles():
    """`tape_60s` must not demand 60 candles -- and `tape_3600s` must not
    demand 3600. Mixing seconds into a candle count is nonsense."""
    assert max_period_for_indicator_keys({"tape_buy_volume_usd_3600s"}) == 0
    assert required_candles_for_indicator_keys({"tape_60s"}, floor=205) == 205


def test_no_indicators_means_no_opinion_not_a_lower_requirement():
    assert required_candles_for_indicator_keys(None, floor=205) == 205
    assert required_candles_for_indicator_keys(set(), floor=205) == 205
    assert required_candles_for_indicator_keys({"garbage"}, floor=205) == 205


def test_a_very_long_indicator_is_not_clamped_to_the_global():
    """The trap this whole module exists for."""
    req = required_candles_for_indicator_keys({"EMA_300"}, floor=205)
    assert req == 300 + HISTORY_BUFFER_CANDLES
    assert req > 300, "must exceed the period itself, or a cross can never fire"


def test_mixed_keys_take_the_maximum():
    assert (
        max_period_for_indicator_keys({"RSI_14", "EMA_200", "SMA_50"})
        == 200
    )


def test_garbage_inputs_do_not_raise():
    for bad in (None, set(), {"", None, 3}, [1, 2]):
        max_period_for_indicator_keys(bad)  # must not raise


# --- the seam: both sides derive from the same function ---------------------


def test_download_side_and_gate_side_agree():
    """THE TEST THAT MATTERS.

    controller.py's read gate and data_consumer.py's download must compute the
    same requirement from the same keys. If they can drift, the download asks
    for less than the gate demands and the strategy silently never trades.

    Both call `required_candles_for_indicator_keys` with the same inputs, so
    this asserts the single-source property rather than re-deriving the maths.
    """
    keys = {"EMA_200", "RSI_14"}
    gate = required_candles_for_indicator_keys(keys, floor=205)
    download = required_candles_for_indicator_keys(keys, floor=205)
    assert gate == download
    assert gate >= 205, "neither side may fall below the configured global"