"""Gate 4, Part A: the strategy registry must contain strategies that RUN.

THE DEFECT
==========
``bot_module/strategy.py`` built its registry with a bare name filter over
``BaseStrategy.__subclasses__()``. A name being on the list was the only
requirement, so a name could be advertised and still be unreachable. Nine
named strategies were in that state: real implementations, real ``NAME``
attributes, and no way for the engine to start them.

WHAT THIS TEST PROVES
=====================
For every strategy the registry now offers:

* it is in the registry, it is a ``BaseStrategy`` subclass, and its ``NAME``
  resolves back through the registry;
* the ENGINE can construct it (``create_strategy_instance`` is the call the
  controller makes);
* it actually RUNS: its own pattern detection fires on a scenario built for
  it, and the full ``check_signal_sync`` path returns a real signal. A
  strategy that is registered but cannot produce a trade is still decorative.

One named strategy is deliberately NOT registered. ``OnlineAgentStrategy``
takes no ``(params, contract_id)`` arguments, so the engine's own
construction call raises ``TypeError`` and every pick of it would fail to
start. That is asserted here rather than left as a comment, so the decision
cannot quietly rot.

The market data below is synthetic but it is built to each strategy's real
requirements (lookback lengths, volume multipliers, timeframe indexes), so a
strategy that stops working shows up as a failure here.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytest

from bot_module.strategy import (
    STRATEGIES,
    BaseStrategy,
    create_strategy_instance,
    get_strategy_instance,
)

#: The strategies this gate puts into the registry, with the pattern each one
#: is expected to detect on the scenario built for it.
#:
#: ``None`` means "this strategy has no named pattern": it is driven by the
#: blocks of the configuration it is given, so what has to hold is that its
#: own configuration fired and produced a trade.
ENABLED_CLASSIC_STRATEGIES: Dict[str, Optional[str]] = {
    "VolumeBreakout": "VolBreakUp",
    "FakeBreakout": "FakeBreakUp",
    "DensityBounce": "DensityBounceLong",
    "ConsolidationImpulse": "ConsImpulseUp",
    "AggTradeReversal": "AggReversalUpSpike",
    "FirstPullbacksInTrend": "PullbackSmaLong",
    "ReverseVolumeBreakout": "VolBreakUp",
    "ReverseFakeBreakout": "FakeBreakUp",
    # The genetic adapter executes the visual blocks of the configuration it
    # is handed (bot_module/genetic_adapter.py), so it has no named pattern.
    "GeneticStrategy": None,
}

#: The configuration the genetic adapter is given. Without it the adapter has
#: nothing to execute, so it is part of the scenario, not an afterthought.
GENETIC_VISUAL_CONFIG: Dict[str, Any] = {
    "name": "Genetic Strategy",
    "min_total_foundation_weight_threshold": 0,
    "entryConditions": {
        "id": "e_root",
        "type": "AND",
        "children": [
            {
                "type": "price_vs_level",
                "params": {
                    "price_source": {"source": "candle", "key": "close"},
                    "operator": "gt",
                    "level_source": {"source": "value", "value": 99},
                },
            }
        ],
    },
    "initialization": {
        "id": "act1",
        "type": "open_position",
        "params": {
            "direction": "LONG",
            "sl_type": "fixed_price",
            "sl_value": 98.0,
            "tp_type": "fixed_price",
            "tp_value": 110.0,
        },
    },
    "positionManagement": [],
}

#: The params a strategy is constructed with. The engine's own call is
#: ``create_strategy_instance(name, params={"enabled": True})``; a strategy
#: that is driven by a configuration needs that configuration.
STRATEGY_INSTANCE_PARAMS: Dict[str, Dict[str, Any]] = {
    "GeneticStrategy": {"enabled": True, "config": GENETIC_VISUAL_CONFIG},
}

#: Named strategies that are deliberately NOT in the registry, and why.
#: The reason is asserted by a test below, so it cannot drift away from the
#: code.
DELIBERATELY_DISABLED: Dict[str, str] = {
    # bot_module/ml_strategy.py:96 -- __init__(self), no params/contract_id.
    "OnlineAgentStrategy": "constructor does not accept (params, contract_id)",
}

#: The nine names that were named but unreachable before this gate.
PREVIOUSLY_BLOCKED_NAMES: Tuple[str, ...] = (
    "AggTradeReversal",
    "ConsolidationImpulse",
    "DensityBounce",
    "FakeBreakout",
    "FirstPullbacksInTrend",
    "OnlineAgentStrategy",
    "ReverseFakeBreakout",
    "ReverseVolumeBreakout",
    "VolumeBreakout",
)

SYMBOL = "TESTUSDT"
TICK_SIZE = 0.01
ATR = 0.5


# --- synthetic market data --------------------------------------------------


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
        "atr": ATR,
        "tick_size": TICK_SIZE,
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
    # A real density on both sides: price * size is well above the 100000 USD
    # floor the order-book foundation uses.
    bids[0] = ["100.00", "3000.0"]
    asks[0] = ["100.50", "3000.0"]
    return {"lastUpdateId": 1, "bids": bids, "asks": asks}


def _agg_trades(spike_price: float = 101.5) -> pd.DataFrame:
    rows = [[100.0, 5.0] for _ in range(15)] + [[spike_price, 20.0] for _ in range(5)]
    index = pd.date_range("2026-01-01", periods=len(rows), freq="1s", tz="UTC")
    frame = pd.DataFrame(rows, columns=["price", "quantity"], index=index)
    frame.index.name = "timestamp"
    return frame


def _market_data(candles: pd.DataFrame) -> Dict[str, Any]:
    book = _depth_book()
    return {
        "kline_1m": candles,
        "kline_3m": _resample(candles, "3min"),
        "kline_5m": _resample(candles, "5min"),
        "kline_30m": _resample(candles, "30min"),
        "depth": book,
        "depth_trading": copy.deepcopy(book),
        "depth_analysis": copy.deepcopy(book),
        "aggTrade": _agg_trades(),
    }


def _volume_breakout_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    rows = _flat(58)
    rows.append([100.0, 100.5, 99.5, 100.2, 1200.0])
    rows.append([100.2, 101.2, 100.1, 101.0, 4000.0])
    candles = _candles(rows)
    return candles, _pair_info(101.0, len(candles) - 1), _market_data(candles)


def _fake_breakout_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    # FakeBreakout looks back 100 candles and wants 2 reversal bars
    # (bot_module/config.py: FakeBreakout lookback_candles / reversal_confirmation_bars).
    rows = _flat(103)
    rows.append([100.0, 101.0, 99.9, 100.8, 2000.0])
    rows.append([100.8, 100.9, 99.5, 100.0, 2500.0])
    rows.append([100.0, 100.6, 99.5, 100.1, 2500.0])
    candles = _candles(rows)
    return candles, _pair_info(100.1, len(candles) - 1), _market_data(candles)


def _density_bounce_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    candles = _candles(_flat(30))
    market = _market_data(candles)
    return candles, _pair_info(100.0, len(candles) - 1), market


def _consolidation_impulse_scenario() -> (
    Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]
):
    # range_bars=8 and entry_delay_bars=2 (bot_module/config.py).
    rows = _flat(30)
    rows.append([100.0, 100.7, 99.9, 100.6, 6000.0])  # impulse
    rows.append([100.6, 100.7, 100.4, 100.5, 2000.0])
    rows.append([100.5, 100.7, 100.4, 100.6, 6000.0])  # trigger, volume spike
    candles = _candles(rows)
    return candles, _pair_info(100.6, len(candles) - 1), _market_data(candles)


def _agg_trade_reversal_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    candles = _candles(_flat(30))
    market = _market_data(candles)
    market["aggTrade"] = _agg_trades(spike_price=101.5)
    return candles, _pair_info(101.5, len(candles) - 1), market


def _first_pullbacks_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    rows = _flat(300)
    # The volume-confirmation foundation reads the 1m series at the current
    # index, so that candle carries the volume spike.
    rows[59] = [100.0, 100.3, 99.9, 100.1, 6000.0]
    candles = _candles(rows)
    market = _market_data(candles)
    five = market["kline_5m"].copy()
    five.iloc[-1, five.columns.get_loc("low")] = 99.9
    market["kline_5m"] = five
    return candles, _pair_info(100.0, len(market["kline_5m"]) - 1), market


def _genetic_scenario() -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    # The genetic adapter runs the blocks of the configuration it is handed:
    # a close above 99 opens a long with a stop at 98 and a target at 110.
    rows = _flat(60)
    rows.append([100.0, 101.0, 99.9, 100.8, 3000.0])
    candles = _candles(rows)
    return candles, _pair_info(100.8, len(candles) - 1), _market_data(candles)


SCENARIOS = {
    "VolumeBreakout": _volume_breakout_scenario,
    "FakeBreakout": _fake_breakout_scenario,
    "DensityBounce": _density_bounce_scenario,
    "ConsolidationImpulse": _consolidation_impulse_scenario,
    "AggTradeReversal": _agg_trade_reversal_scenario,
    "FirstPullbacksInTrend": _first_pullbacks_scenario,
    "ReverseVolumeBreakout": _volume_breakout_scenario,
    "ReverseFakeBreakout": _fake_breakout_scenario,
    "GeneticStrategy": _genetic_scenario,
}


# --- the registry -----------------------------------------------------------


def test_registry_offers_every_enabled_strategy():
    """Every strategy this gate enables is in the registry and resolves back."""
    for name in ENABLED_CLASSIC_STRATEGIES:
        assert name in STRATEGIES, f"{name} is missing from the strategy registry"
        cls = STRATEGIES[name]
        assert issubclass(cls, BaseStrategy)
        assert cls.NAME == name
        assert STRATEGIES[cls.NAME] is cls


def test_registry_is_not_the_old_two_entry_list():
    """The registry used to hold 2 entries. It must not go back to that."""
    old_registry_size = 2
    assert len(STRATEGIES) > old_registry_size
    for name in ENABLED_CLASSIC_STRATEGIES:
        assert name in STRATEGIES


@pytest.mark.parametrize("name", sorted(ENABLED_CLASSIC_STRATEGIES))
def test_enabled_strategy_constructs_through_the_engine(name):
    """The engine's own construction call must work for every enabled name."""
    instance = create_strategy_instance(
        name, params=STRATEGY_INSTANCE_PARAMS.get(name, {"enabled": True})
    )
    assert instance is not None, f"{name} could not be constructed by the engine"
    assert isinstance(instance, BaseStrategy)
    assert instance.NAME == name
    assert instance.enabled is True


@pytest.mark.parametrize("name", sorted(ENABLED_CLASSIC_STRATEGIES))
def test_enabled_strategy_detects_its_own_pattern(name):
    """The strategy's own pattern check must fire on a scenario built for it."""
    expected = ENABLED_CLASSIC_STRATEGIES[name]
    instance = create_strategy_instance(
        name, params=STRATEGY_INSTANCE_PARAMS.get(name, {"enabled": True})
    )
    _, pair_info, market = SCENARIOS[name]()
    if expected is None:
        # A configuration-driven strategy has no named pattern: what detects
        # the setup is the blocks in its own configuration, so the assertion is
        # that those blocks fired.
        signal, _, _ = instance.check_signal_sync(pair_info, market)
        assert signal is not None, f"{name} produced no signal on its own scenario"
        assert signal.details["decision_trace"]["result"] is True
        return
    foundations, _ = instance.check_foundations(pair_info, market)
    assert foundations.get("pattern_detected") == expected


@pytest.mark.parametrize("name", sorted(ENABLED_CLASSIC_STRATEGIES))
def test_enabled_strategy_emits_a_signal_end_to_end(name):
    """A registered strategy that never trades is still decorative.

    This runs the whole path the bot runs: check_signal_sync -> foundations ->
    the strategy's own signal logic -> the foundation weight gate.
    """
    instance = create_strategy_instance(
        name, params=STRATEGY_INSTANCE_PARAMS.get(name, {"enabled": True})
    )
    _, pair_info, market = SCENARIOS[name]()
    signal, weight, trace = instance.check_signal_sync(pair_info, market)
    assert signal is not None, f"{name} produced no signal on its own scenario"
    assert signal.direction is not None
    assert signal.stop_loss is not None
    assert trace
    if ENABLED_CLASSIC_STRATEGIES[name] is None:
        # A configuration-driven strategy has no named pattern and no
        # "expensive signal" weight; what has to hold is that its own
        # configuration fired.
        assert signal.details["decision_trace"]["result"] is True
    else:
        assert weight > 0


def test_the_genetic_adapter_went_through_the_registry_verification():
    """The genetic adapter must be VERIFIED, not just registered.

    ``bot_module/strategy.py`` used to drop the adapter into the registry
    without asking the same questions it asks every other candidate, so a
    broken adapter would still be offered to a client -- and a client that
    picked it would get a strategy that cannot start.
    """
    from bot_module.genetic_adapter import GeneticCompatibleStrategy
    from bot_module.strategy import _verify_registry_candidate

    assert GeneticCompatibleStrategy.NAME in STRATEGIES
    assert STRATEGIES[GeneticCompatibleStrategy.NAME] is GeneticCompatibleStrategy
    assert _verify_registry_candidate(GeneticCompatibleStrategy) is None


def test_online_agent_strategy_is_deliberately_not_registered():
    """The one named strategy left out, and the reason it cannot be used.

    The engine constructs a strategy with ``cls(params=..., contract_id=...)``.
    ``OnlineAgentStrategy.__init__`` takes neither, so the call raises and
    ``create_strategy_instance`` returns None. Registering it would give a
    client a strategy that cannot start.
    """
    assert "OnlineAgentStrategy" not in STRATEGIES
    instance = create_strategy_instance("OnlineAgentStrategy", params={"enabled": True})
    assert instance is None

    from bot_module.ml_strategy import OnlineAgentStrategy

    with pytest.raises(TypeError):
        OnlineAgentStrategy(params={}, contract_id=None)


def test_every_previously_blocked_name_is_accounted_for():
    """No name may be silently dropped: each is enabled or explicitly not."""
    for name in PREVIOUSLY_BLOCKED_NAMES:
        enabled = name in ENABLED_CLASSIC_STRATEGIES
        disabled = name in DELIBERATELY_DISABLED
        assert enabled or disabled, f"{name} is neither enabled nor explained"
        if enabled:
            assert name in STRATEGIES
        else:
            assert name not in STRATEGIES


def test_first_pullbacks_subscribes_to_the_series_its_volume_foundation_reads():
    """The volume-confirmation foundation reads kline_1m by default.

    The strategy's signal logic REQUIRES that foundation, and the controller
    hands a strategy only the market-data keys it asked for. Without kline_1m
    in the request the foundation is always False and the strategy never
    signals -- it would be registered but dead.
    """
    instance = create_strategy_instance("FirstPullbacksInTrend", params={"enabled": True})
    assert "kline_1m" in instance.required_data_types


def test_shipped_partial_exit_config_leaves_the_base_signal_without_a_take_profit():
    """Why the reverse strategies need their own base instance.

    The partial-exit configuration shipped in bot_module/config.py covers
    100% of the position, so the base signal has no take-profit at all. The
    reverse strategies build their trade from that take-profit, so with the
    shared instance they could never produce a signal.
    """
    instance = create_strategy_instance("VolumeBreakout", params={"enabled": True})
    _, pair_info, market = _volume_breakout_scenario()
    foundations, _ = instance.check_foundations(pair_info, market)
    signal = instance._check_specific_signal_logic(pair_info, market, foundations)
    assert signal is not None
    assert signal.take_profit is None

    shared = get_strategy_instance("VolumeBreakout")
    assert shared is not None
