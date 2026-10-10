"""
Regression tests for ma_cross_condition direction handling (2026-10-10).

The defect
----------
`evaluate_ma_cross_scalar` hardcoded

    result = (f0 > s0) and (f1 <= s1)

which is a GOLDEN cross, with no reference to `direction`. Meanwhile
`VisualBuilderStrategy._get_all_required_indicators_from_json` declared
`SMA_<period>` for `ma_cross_condition`, while
`_check_condition_ma_cross` reads `EMA_<period>` columns. The runtime
therefore never computed the EMA columns, the lookup returned None, and
EVERY cross fell through to the direction-blind scalar helper.

Consequence: a strategy whose EXIT gate was `cross_below` (death cross)
had that exit evaluated as `cross_above` -- the same event as its entry.
The position opened and closed on the same crossover.

These tests cover both links in that chain, plus the negative control that
makes the positive assertion mean something.
"""

import pandas as pd

from bot_module.condition_core import evaluate_ma_cross_scalar
from bot_module.strategy import VisualBuilderStrategy


def _golden_cross_closes():
    """
    A cross is a TRANSIENT -- it only registers on the single bar where the
    relationship between the two averages actually flips. A smooth trend
    crosses early and then stays crossed, so it never satisfies the
    transition test on the final bar.

    Build it deliberately: a gentle DOWNWARD drift leaves the fast EMA just
    below the slow EMA, then one large final bar flips it.
    """
    closes = [100.0 - i * 0.005 for i in range(259)]
    closes.append(closes[-1] + 20.0)
    return closes


def _death_cross_closes():
    """Mirror: gentle UPWARD drift leaves fast just above slow, then one large drop."""
    closes = [100.0 + i * 0.005 for i in range(259)]
    closes.append(closes[-1] - 20.0)
    return closes


def _df(closes):
    return pd.DataFrame({"close": closes})


_FAST, _SLOW = 5, 50


# ---------------------------------------------------------------- the defect


def test_death_cross_triggers_a_cross_below_exit():
    """The positive assertion: a real death cross must fire a cross_below gate."""
    fired, meta = evaluate_ma_cross_scalar(
        _df(_death_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_below"},
    )

    # Self-validating: confirm the fixture really is a death cross, so a green
    # result cannot come from a cross that never happened.
    assert meta["fast"] < meta["slow"], "fixture did not produce a death cross"
    assert fired is True


def test_golden_cross_does_not_trigger_a_cross_below_exit():
    """
    THE NEGATIVE CONTROL. This is the bug.

    A golden cross must NOT fire a cross_below gate. On the old code this
    returned True -- the exit fired on the entry event, and the strategy
    round-tripped instead of holding.
    """
    fired, meta = evaluate_ma_cross_scalar(
        _df(_golden_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_below"},
    )

    assert meta["fast"] > meta["slow"], "fixture did not produce a golden cross"
    assert fired is False


def test_golden_cross_triggers_a_cross_above_entry():
    """The entry side still works -- guards against fixing the exit by inverting both."""
    fired, meta = evaluate_ma_cross_scalar(
        _df(_golden_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_above"},
    )

    assert meta["fast"] > meta["slow"]
    assert fired is True


def test_death_cross_does_not_trigger_a_cross_above_entry():
    """Mirror control on the entry side."""
    fired, _ = evaluate_ma_cross_scalar(
        _df(_death_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_above"},
    )

    assert fired is False


def test_legacy_direction_words_are_still_honoured():
    """'Above'/'Below' are the words the visual editor emits."""
    _, above = evaluate_ma_cross_scalar(
        _df(_golden_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "Above"},
    )
    fired_below, _ = evaluate_ma_cross_scalar(
        _df(_death_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "Below"},
    )

    assert above["direction"] == "Above"
    assert fired_below is True


# ------------------------------------------------- the degraded paths, loudly


def test_single_candle_reports_an_error_not_a_bare_false():
    """
    iloc[-2] raises IndexError on a one-row frame. That used to be swallowed by
    the blanket except and reported as an ordinary "condition false" -- which is
    indistinguishable, in the logs, from a genuine no-signal.
    """
    fired, meta = evaluate_ma_cross_scalar(
        _df([100.0]), {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_above"}
    )

    assert fired is False
    assert "error" in meta, "degraded path must be loud, not silently False"
    assert "need 2" in meta["error"]


def test_unknown_direction_reports_an_error_instead_of_defaulting_to_a_golden_cross():
    """An unrecognised direction must not quietly become a golden cross."""
    fired, meta = evaluate_ma_cross_scalar(
        _df(_golden_cross_closes()),
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "sideways"},
    )

    assert fired is False
    assert "error" in meta
    assert "sideways" in meta["error"]


# ---------------------------------------------------- the seam between the two


def test_declared_indicators_are_the_ones_the_evaluator_reads():
    """
    THE SEAM TEST.

    Two components were each individually plausible: the strategy declared
    SMA_50/SMA_200, the evaluator read EMA_50/EMA_200. Nothing tested the join,
    and the join is where it broke.

    If the declaration drifts back to SMA, the runtime computes SMA columns,
    the EMA lookup returns None, and every cross silently falls through to the
    direction-blind scalar helper -- reintroducing the exact bug above while
    every other test in this file stays green.
    """
    config = {
        "entryConditions": {
            "type": "AND",
            "children": [
                {
                    "type": "ma_cross_condition",
                    "params": {
                        "fast_period": 50,
                        "slow_period": 200,
                        "direction": "cross_above",
                    },
                }
            ],
        },
        "positionManagement": {
            "type": "AND",
            "children": [
                {
                    "if_conditions": {
                        "type": "AND",
                        "children": [
                            {
                                "type": "ma_cross_condition",
                                "params": {
                                    "fast_period": 50,
                                    "slow_period": 200,
                                    "direction": "cross_below",
                                },
                            }
                        ],
                    },
                    "then_actions": [{"type": "close_position", "params": {}}],
                }
            ],
        },
    }

    required = VisualBuilderStrategy(params={"config": config}).required_indicators

    assert "EMA_50" in required, (
        "the ma_cross evaluator reads EMA_<period>; declaring anything else "
        "leaves every cross on the direction-blind fallback path"
    )
    assert "EMA_200" in required
    assert "SMA_50" not in required
    assert "SMA_200" not in required


def test_genetic_adapter_ma_cross_also_honours_direction():
    """
    The SAME defect existed a second time, in GeneticCompatibleStrategy's own
    copy of the handler (bot_module/genetic_adapter.py), which never called
    evaluate_ma_cross_scalar and so was not covered by the fix above.

    This is the offline genetic-search path, not live trading -- but a
    cross_below exit scoring as a cross_above misleads the search into
    selecting strategies that would not behave as written.
    """
    from bot_module.genetic_adapter import GeneticCompatibleStrategy

    adapter = GeneticCompatibleStrategy(params={"config": {}})
    pair_info = {"candle_timeframe": "4h"}
    ctx = {}

    fired_below, _ = adapter._check_condition_ma_cross(
        pair_info,
        {"kline_4h": _df(_death_cross_closes())},
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_below"},
        ctx,
    )
    assert fired_below is True

    fired_above, _ = adapter._check_condition_ma_cross(
        pair_info,
        {"kline_4h": _df(_golden_cross_closes())},
        {"fast_period": _FAST, "slow_period": _SLOW, "direction": "cross_below"},
        ctx,
    )
    assert fired_above is False, (
        "a golden cross must not satisfy a cross_below gate here either"
    )