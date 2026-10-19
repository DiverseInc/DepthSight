"""A screener outage must be LOUD when a risk control depends on that feed.

WHY
---
`breakeven_on_regime_change` is implemented in
`check_on_screener_update` (strategy.py:3070-3079), which is only ever reached
from `_dynamic_symbol_selection_loop` (controller.py:1236) -- fed exclusively by
the `Main_app_ws` socket. `_dynamic_symbol_selection_loop` runs for EVERY
controller, STATIC and FIXED included; the DYNAMIC guard at strategies.py:566
only refuses DYNAMIC strategies at START and says nothing about this.

So while the feed is down, every running strategy with that option set has its
regime-change stop silently inert. `data_consumer` used to escalate only on
`SYMBOL_SELECTION_ALLOW_DYNAMIC` and log, at INFO, that "Pinned / STATIC / FIXED
strategies are unaffected" -- which was false.

These drive the real method on a real-ish consumer.
"""
import logging

import pytest

from bot_module.data_consumer import DataConsumer


class _Strategy:
    def __init__(self, breakeven):
        self.breakeven_on_regime_change = breakeven


class _Controller:
    def __init__(self, instances):
        self.running_strategy_instances = instances


def _make_consumer(instances):
    dc = DataConsumer.__new__(DataConsumer)
    dc.controller = _Controller(instances) if instances is not None else None
    return dc


def test_no_strategies_means_no_risk_control_at_risk():
    dc = _make_consumer({})
    assert dc._regime_change_controls_at_risk() is False


def test_no_controller_at_all_is_not_at_risk():
    """A missing controller must not escalate -- and must not raise."""
    dc = _make_consumer(None)
    assert dc._regime_change_controls_at_risk() is False


def test_a_strategy_without_the_option_is_not_at_risk():
    dc = _make_consumer({"cfg-1": (_Strategy(False), "other")})
    assert dc._regime_change_controls_at_risk() is False


def test_a_regime_change_strategy_is_at_risk():
    dc = _make_consumer({"cfg-1": (_Strategy(True), "other")})
    assert dc._regime_change_controls_at_risk() is True


def test_one_at_risk_strategy_among_many_is_detected():
    dc = _make_consumer(
        {
            "cfg-1": (_Strategy(False), "a"),
            "cfg-2": (_Strategy(True), "b"),
            "cfg-3": (_Strategy(False), "c"),
        }
    )
    assert dc._regime_change_controls_at_risk() is True


def test_a_non_tuple_entry_is_tolerated():
    """The controller stores tuples today; a bare instance must not crash it."""
    dc = _make_consumer({"cfg-1": _Strategy(True)})
    assert dc._regime_change_controls_at_risk() is True


def test_it_never_raises_even_when_the_controller_is_broken():
    class _Exploding:
        @property
        def running_strategy_instances(self):
            raise RuntimeError("controller exploded")

    dc = DataConsumer.__new__(DataConsumer)
    dc.controller = _Exploding()
    # A logger must never be able to break the WS loop or take down market data.
    assert dc._regime_change_controls_at_risk() is False


def test_an_empty_tuple_entry_does_not_abort_the_scan():
    """An empty tuple would IndexError; it must not hide a real one behind it."""
    dc = _make_consumer(
        {
            "cfg-1": (),
            "cfg-2": (_Strategy(True), "b"),
        }
    )
    assert dc._regime_change_controls_at_risk() is True


def test_a_malformed_entry_does_not_abort_the_scan():
    """One bad entry must not hide a real one behind it."""

    class _Weird:
        pass

    dc = _make_consumer(
        {
            "cfg-1": _Weird(),  # no such attribute -> getattr default False
            "cfg-2": (_Strategy(True), "b"),
        }
    )
    assert dc._regime_change_controls_at_risk() is True
