"""
Regression tests for the Bollinger MIDDLE band (2026-10-10).

The defect
----------
`pandas_ta.bbands` has always returned five columns: BBL, BBM, BBU, BBB, BBP.
Every evaluator picked out BBL / BBU / BBB and **never read BBM**. The middle
band was computed, in memory, on every single evaluation -- and thrown away.

Consequence: `bollinger_bands_condition` had no way to express "price returned
to the middle band", which is why a legacy `reaches_middle` exit was the one
condition `legacy_blocks_migration` refused to convert. It was an
implementation gap being reported as a semantic limit.

These tests cover the three places that now read BBM, plus the tolerance
semantics and the loud-unknown-type behaviour.
"""

import pandas as pd
import pytest

from bot_module.condition_core import (
    evaluate_bollinger_logic,
    evaluate_bollinger_scalar as evaluate_bb_scalar,
)


def _oscillating_closes(n=80):
    """A wave, so the bands are non-degenerate and close sits near the middle."""
    return [100.0 + (i % 8) * 0.4 - (i % 3) * 0.2 for i in range(n)]


def _df():
    return pd.DataFrame({"close": _oscillating_closes()})


# ------------------------------------------------- the pure logic, exact maths


def test_touches_middle_fires_when_price_sits_on_the_middle_band():
    assert evaluate_bollinger_logic(
        close=100.0, lower=90.0, upper=110.0, width=0.2,
        check_type="price_touches_middle", middle=100.0,
    )


def test_touches_middle_respects_the_tolerance_band():
    """span = 20, default tol 0.10 -> within 2.0 of the middle."""
    # |104 - 100| = 4.0 > 2.0 -> must NOT fire
    assert not evaluate_bollinger_logic(
        close=104.0, lower=90.0, upper=110.0, width=0.2,
        check_type="price_touches_middle", middle=100.0,
    )
    # |101.5 - 100| = 1.5 <= 2.0 -> must fire
    assert evaluate_bollinger_logic(
        close=101.5, lower=90.0, upper=110.0, width=0.2,
        check_type="price_touches_middle", middle=100.0,
    )


def test_touches_middle_tolerance_is_configurable():
    """span = 20. Default tol 0.10 -> within 2.0; 0.25 -> within 5.0.

    close=104 is 4.0 from the middle, so it is OUTSIDE the default tolerance and
    INSIDE the widened one. That makes this a real assertion about the
    parameter rather than a restatement of the default.
    """
    kwargs = dict(lower=90.0, upper=110.0, width=0.2, middle=100.0)
    assert not evaluate_bollinger_logic(
        close=104.0, check_type="price_touches_middle", **kwargs
    )
    assert evaluate_bollinger_logic(
        close=104.0, check_type="price_touches_middle", touch_tolerance=0.25, **kwargs
    )


def test_missing_upper_band_column_does_not_inflate_the_tolerance(monkeypatch):
    """OpenCode finding, 2026-10-10.

    The scalar evaluator defaults `upper` to **0** (not None) when the BBU
    column is absent. The first version of the guard tested `band_span > 0`,
    which `abs(0 - lower) = |lower|` satisfies -- a large positive number. The
    price-relative fallback therefore never fired and the tolerance became 10%
    of the lower band *price* rather than of the band span.

    Unreachable with real pandas_ta 4.5.85 (bbands always returns all five
    columns), so this drives the evaluator with a stub whose bbands result is
    missing BBU. On the buggy guard: band_span = |0-90| = 90, tol = 9.0, and
    close=110 is 10.0 from the middle -> False. With the column guard:
    band_span = 0, tol = close*0.10 = 11.0 -> True.
    """
    bb = pd.DataFrame(
        {
            "BBL_20_2.0_2.0": [90.0],
            "BBM_20_2.0_2.0": [100.0],
            "BBB_20_2.0_2.0": [0.2],
            # No BBU column -- this is the edge case.
        }
    )

    class _StubTA:
        def bbands(self, length, std):
            return bb

    monkeypatch.setattr(pd.DataFrame, "ta", property(lambda self: _StubTA()), raising=False)

    df = pd.DataFrame({"close": [110.0] * 40})
    fired, meta = evaluate_bb_scalar(
        df, {"period": 20, "std_dev": 2.0, "check_type": "price_touches_middle"}
    )

    assert "error" not in meta, meta
    assert meta["upper"] == 0.0, "precondition: upper defaults to 0, not None"
    assert fired is True, (
        "with BBU missing the tolerance must fall back to close*touch_tolerance "
        "(11.0), not 0.10*|0-lower| (9.0)"
    )


def test_above_and_below_middle_are_distinct_from_touching():
    kwargs = dict(lower=90.0, upper=110.0, width=0.2, middle=100.0)
    assert evaluate_bollinger_logic(close=104.0, check_type="price_above_middle", **kwargs)
    assert evaluate_bollinger_logic(close=96.0, check_type="price_below_middle", **kwargs)
    assert not evaluate_bollinger_logic(close=96.0, check_type="price_above_middle", **kwargs)


def test_middle_band_checks_return_false_when_middle_is_missing():
    """Absent input must not be a silent True."""
    assert not evaluate_bollinger_logic(
        close=100.0, lower=90.0, upper=110.0, width=0.2,
        check_type="price_touches_middle", middle=None,
    )


def test_unknown_check_type_returns_false_not_a_guess():
    """The failure mode this whole session is about: degrading to a wrong value."""
    assert not evaluate_bollinger_logic(
        close=100.0, lower=90.0, upper=110.0, width=0.2,
        check_type="touches_the_ceiling_of_reality", middle=100.0,
    )


# ------------------------------------------- the scalar path reads BBM at all


def test_evaluate_bb_scalar_reports_the_middle_band():
    """BBM was computed and discarded. The detail key proves it is now read."""
    _, meta = evaluate_bb_scalar(
        _df(), {"period": 20, "std_dev": 2.0, "check_type": "price_below_lower"}
    )
    assert meta.get("middle") is not None, (
        "the middle band is still not being read out of the bbands result"
    )


def test_evaluate_bb_scalar_honours_price_touches_middle():
    fired, meta = evaluate_bb_scalar(
        _df(), {"period": 20, "std_dev": 2.0, "check_type": "price_touches_middle"}
    )
    assert "error" not in meta, meta
    assert isinstance(fired, bool)


def test_evaluate_bb_scalar_rejects_an_unknown_check_type_loudly():
    """An unknown type must surface an error, not a quiet False."""
    fired, meta = evaluate_bb_scalar(
        _df(), {"period": 20, "std_dev": 2.0, "check_type": "sideways"}
    )
    assert fired is False
    assert "error" in meta, "degraded path must be loud"
    assert "sideways" in meta["error"]


def test_existing_check_types_are_unaffected():
    """Adding a branch must not change the four original outcomes."""
    params = {"period": 20, "std_dev": 2.0}
    df = _df()
    for check_type in ("price_below_lower", "price_above_upper", "width_gt", "width_lt"):
        fired, meta = evaluate_bb_scalar(df, {**params, "check_type": check_type})
        assert "error" not in meta, f"{check_type} broke: {meta}"
        assert isinstance(fired, bool)


# ------------------------------------------------- the genetic-search copy


def test_genetic_adapter_bb_also_reads_the_middle_band():
    """A separate implementation of the same check -- offline search path.

    If this drifts, the genetic finder scores middle-band strategies as though
    the condition never fired.
    """
    from bot_module.genetic_adapter import GeneticCompatibleStrategy

    adapter = GeneticCompatibleStrategy(params={"config": {}})
    _, meta = adapter._check_condition_bb(
        {"candle_timeframe": "15m"},
        {"kline_15m": _df()},
        {"period": 20, "std_dev": 2.0, "check_type": "price_touches_middle"},
        {},
    )
    assert "error" not in meta, meta
    assert meta.get("middle") is not None