"""Gate 4, Part D: the bot must honour what the client picked -- and say so.

THE DEFECT
==========
The two "Reverse*" strategies borrow another strategy's signal and build their
trade from the ORIGINAL signal's take-profit. Three things were wrong:

* the borrowed instance was rebuilt from scratch on EVERY candle -- the
  parameters it is built from cannot change while it runs, so this was pure
  waste on the hot path;
* the client's own parameters (``config_data.strategy_params``) never
  reached the borrowed strategy, so a client that asked for a timeframe got
  the config default instead;
* a partial-exit configuration that covers 100% of the position leaves the
  original signal with NO take-profit at all. The reverse is built from that
  take-profit, so such a configuration put the strategy into a state where it
  could never fire -- silently, for every client who picked it.

And one thing was invisible: a strategy that is advertised but did not make it
into the registry was logged at INFO, which the default log level does not
show, so a dropped candidate left no trace at all.

WHAT THIS TEST PROVES
=====================
* the borrowed strategy gets the client's parameters;
* it is built ONCE, not again on every candle;
* a partial-exit configuration that covers the whole position is dropped, so
  the reverse still produces a signal;
* a dropped candidate is logged at WARNING, so it is visible at the default
  log level.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple
from unittest import mock

import pandas as pd
import pytest

import bot_module.strategy as strategy_module
from bot_module.strategy import create_strategy_instance

SYMBOL = "TESTUSDT"


# --- synthetic market data (the volume-breakout scenario) --------------------


def _candles(rows: List[List[float]]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(rows), freq="1min", tz="UTC")
    frame = pd.DataFrame(
        rows, columns=["open", "high", "low", "close", "volume"], index=index
    )
    frame.index.name = "open_time"
    return frame


def _flat(count: int) -> List[List[float]]:
    return [[100.0, 100.2, 99.8, 100.0, 1000.0] for _ in range(count)]


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = frame.resample(rule).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return out.dropna()


def _pair_info(last_price: float, index: int) -> Dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "current_candle_index": index,
        "atr": 0.5,
        "tick_size": 0.01,
        "last_price": last_price,
        "price": last_price,
        "high": last_price + 0.1,
        "low": last_price - 0.1,
        "volume": 1000.0,
        "SMA_10": 100.0,
        "SMA_50": 99.0,
        "RSI_14": 60.0,
        "relative_volume": 3.0,
        "natr": 1.2,
        "is_live_mode": False,
    }


def _depth_book() -> Dict[str, Any]:
    bids = [[f"{100.0 - i * 0.05:.2f}", "1000.0"] for i in range(10)]
    asks = [[f"{100.5 + i * 0.05:.2f}", "1000.0"] for i in range(10)]
    bids[0] = ["100.00", "3000.0"]
    asks[0] = ["100.50", "3000.0"]
    return {"lastUpdateId": 1, "bids": bids, "asks": asks}


def _market_data(candles: pd.DataFrame) -> Dict[str, Any]:
    book = _depth_book()
    return {
        "kline_1m": candles,
        "kline_3m": _resample(candles, "3min"),
        "kline_5m": _resample(candles, "5min"),
        "kline_30m": _resample(candles, "30min"),
        "depth": book,
        "depth_trading": book,
        "depth_analysis": book,
    }


def _volume_breakout_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    rows = _flat(58)
    rows.append([100.0, 100.5, 99.5, 100.2, 1200.0])
    rows.append([100.2, 101.2, 100.1, 101.0, 4000.0])
    candles = _candles(rows)
    return candles, _pair_info(101.0, len(candles) - 1), _market_data(candles)


#: A partial-exit configuration that closes the WHOLE position: the fractions
#: add up to 1.0, so the original signal has no take-profit at all.
FULLY_COVERING_PARTIAL_EXITS = [[0.7, 0.2], [1.0, 0.3], [1.5, 0.25], [2.5, 0.25]]


# --- item 7: the client's parameters, and one build instead of one per candle --


def test_the_borrowed_strategy_gets_the_clients_parameters(monkeypatch):
    """The client's own parameters must reach the borrowed strategy."""
    built: List[str] = []
    original = strategy_module.create_strategy_instance

    def counting_create(name, params=None, contract_id=None):
        built.append(name)
        return original(name, params=params, contract_id=contract_id)

    monkeypatch.setattr(strategy_module, "create_strategy_instance", counting_create)

    instance = strategy_module.create_strategy_instance(
        "ReverseVolumeBreakout",
        params={"enabled": True, "strategy_params": {"candle_timeframe": "5m"}},
    )
    assert instance is not None
    built.clear()  # the construction of the reverse itself is not the borrowed one

    borrowed = instance._original_strategy()
    assert borrowed is not None
    assert borrowed.NAME == "VolumeBreakout"
    assert borrowed._instance_params.get("candle_timeframe") == "5m"
    assert built.count("VolumeBreakout") == 1


def test_the_borrowed_strategy_is_built_once_and_not_again_on_every_candle(
    monkeypatch,
):
    """It used to be rebuilt from scratch on every single candle."""
    built: List[str] = []
    original = strategy_module.create_strategy_instance

    def counting_create(name, params=None, contract_id=None):
        built.append(name)
        return original(name, params=params, contract_id=contract_id)

    monkeypatch.setattr(strategy_module, "create_strategy_instance", counting_create)

    instance = strategy_module.create_strategy_instance(
        "ReverseVolumeBreakout",
        params={"enabled": True, "strategy_params": {"candle_timeframe": "5m"}},
    )
    assert instance is not None
    built.clear()

    _, pair_info, market = _volume_breakout_scenario()
    for _ in range(3):  # three candles
        instance.check_signal_sync(pair_info, market)

    assert built.count("VolumeBreakout") == 1


# --- item 8: a partial-exit configuration that covers everything is dropped ---


def test_a_partial_exit_config_that_covers_the_whole_position_is_dropped():
    """Such a configuration leaves the original signal with NO take-profit."""
    instance = create_strategy_instance(
        "ReverseVolumeBreakout",
        params={
            "enabled": True,
            "partial_exit_rr_config": FULLY_COVERING_PARTIAL_EXITS,
        },
    )
    assert instance is not None

    borrowed = instance._original_strategy()
    assert borrowed is not None
    assert borrowed._instance_params.get("partial_exit_rr_config") is None


def test_a_reverse_still_signals_when_the_client_asks_for_a_fully_covering_partial_exit():
    """The whole point: the reverse must still be able to fire."""
    instance = create_strategy_instance(
        "ReverseVolumeBreakout",
        params={
            "enabled": True,
            "partial_exit_rr_config": FULLY_COVERING_PARTIAL_EXITS,
        },
    )
    assert instance is not None

    _, pair_info, market = _volume_breakout_scenario()
    signal, _, _ = instance.check_signal_sync(pair_info, market)
    assert signal is not None, "the reverse could not fire at all"
    assert signal.direction is not None


def test_a_partial_exit_config_that_leaves_room_for_a_final_target_is_kept():
    """A configuration the client asked for explicitly is honoured."""
    partials = [[1.0, 0.3], [2.0, 0.3]]
    instance = create_strategy_instance(
        "ReverseVolumeBreakout",
        params={"enabled": True, "partial_exit_rr_config": partials},
    )
    assert instance is not None

    borrowed = instance._original_strategy()
    assert borrowed is not None
    assert borrowed._instance_params.get("partial_exit_rr_config") == partials


# --- item 9: a dropped candidate must be visible at the default log level ----


def test_a_dropped_candidate_is_logged_at_warning_level(caplog, monkeypatch):
    """A candidate that does not make it into the registry must SAY SO.

    INFO is not shown at the default log level, so a dropped candidate was
    invisible. The contract is not "this exact sentence is logged": a
    candidate can be dropped for more than one reason -- it may not be a
    subclass at import time, or it may fail the verification -- and the
    reason changes as the registry changes. The contract is that the drop is
    VISIBLE and that it NAMES the candidate.

    CORRECTION (2026-10-31, round 21): this test used to assert the literal
    string "not a direct BaseStrategy subclass". That assertion depended on
    the registry NOT yet having imported the candidate's own module, so it
    passed or failed depending on what else had been imported first -- it
    was a statement about import order, not about the contract. The mechanism
    is now tested directly: a candidate the verification refuses is named in
    a WARNING-or-above record, whatever the reason was.
    """
    import bot_module.strategy as strategy_module

    logger = logging.getLogger("bot_module.strategy")
    caplog.set_level(logging.WARNING, logger="bot_module.strategy")

    real_verify = strategy_module._verify_registry_candidate

    def refuse(cls):
        if getattr(cls, "NAME", None) == "VolumeBreakout":
            return "refused on purpose, for this test"
        return real_verify(cls)

    monkeypatch.setattr(strategy_module, "_verify_registry_candidate", refuse)

    # The registry is global and the builder rewrites it, so the state is put
    # back afterwards: this test must not leave VolumeBreakout missing for
    # the rest of the session.
    saved = dict(strategy_module.STRATEGIES)
    try:
        with mock.patch.object(logger, "propagate", True):
            strategy_module._build_strategy_registry()
    finally:
        strategy_module.STRATEGIES.clear()
        strategy_module.STRATEGIES.update(saved)

    messages = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any(
        "VolumeBreakout" in m and "refused on purpose, for this test" in m
        for m in messages
    ), messages
