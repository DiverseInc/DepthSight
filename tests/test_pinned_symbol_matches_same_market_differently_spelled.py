# tests/test_pinned_symbol_matches_same_market_differently_spelled.py
"""
A pinned strategy must match its own market even when the event spells that
market differently.

Live evidence, 2026-10-14 (immediately after 74086cd fixed the valid-symbols
gate, which finally let candles reach this code path at all):

    17:01:39 [RedisMarketData] subscribed to channel=
        depthsight:market_data:events:okx:futures_usdtm:btc-usdt@kline_1h
    17:01:40 [HistDownload:okx:futures_usdtm:BTC-USDT:1h]
        Fetched 216 1h candles ... Successfully loaded/cached 216
    17:02:03 [ControllerEvent:BTC-USDT] Received CANDLE_CLOSE timeframe=1m
    17:02:03 [SignalCheck:BTC-USDT] No applicable strategy instances ...
        VisualBuilderStrategy: pinned to BTCUSDT, event symbol=BTC-USDT
        VisualBuilderStrategy: pinned to ETHUSDT, event symbol=BTC-USDT

The fix upstream meant `BTC-USDT` finally subscribed and downloaded history,
and then `_select_applicable_instances` threw every candle away, because the
pin holds the LEGACY form ("BTCUSDT") while the event carries the
exchange-native form ("BTC-USDT").

This is the same defect as the valid-symbols gate (74086cd): a verbatim
comparison between a legacy-keyed value and an exchange-native one. Fixing
the gate alone moved the failure one layer down rather than removing it.

These tests drive the REAL matcher via a stub `self` that binds the production
helpers, so they exercise the shipped logic rather than a copy of it.
"""

import pytest

from bot_module.controller import TradingController


def _stub_controller():
    """Minimal stand-in exposing only what the matcher touches.

    The matcher calls three things on `self`; everything else it needs comes
    from the arguments. Binding the real methods keeps this a behavioural test.
    """

    class _Stub:
        _symbols_refer_to_same_market = staticmethod(
            TradingController._symbols_refer_to_same_market
        )
        _select_applicable_instances = TradingController._select_applicable_instances

        def _market_type_for_strategy_config(self, config_dict):
            return "futures_usdtm"

        def _resolve_symbol_pool_for_strategy(self, config_dict):
            mode = str(
                config_dict.get("symbol_selection_mode") or "DYNAMIC"
            ).strip().upper()
            if mode in ("STATIC", "FIXED"):
                return mode, list(config_dict.get("symbols") or []), True
            return mode, [], True

    return _Stub()


def _config(pinned, symbols=("BTCUSDT",)):
    return {
        "symbol_selection_mode": "FIXED",
        "symbols": list(symbols),
        "config_data": {"symbol": pinned, "entryTrigger": {"timeframe": "1m"}},
    }


def _event(timeframe="1m"):
    return {"type": "CANDLE_CLOSE", "timeframe": timeframe}


def _select(pinned, event_symbol, symbols=("BTCUSDT",), timeframe="1m"):
    stub = _stub_controller()
    instance = type("VisualBuilderStrategy", (), {"NAME": "VisualBuilderStrategy"})()
    return stub._select_applicable_instances(
        [(instance, _config(pinned, symbols))],
        event_symbol,
        _event(timeframe),
        "futures_usdtm",
    )


# --- the live failure -------------------------------------------------------


@pytest.mark.parametrize(
    "pinned,event_symbol",
    [
        ("BTCUSDT", "BTC-USDT"),  # <-- the exact 2026-10-14 production line
        ("ETHUSDT", "ETH-USDT"),
        ("BTCUSDT", "BTC-USDT-SWAP"),
        ("BTCUSDT", "BTC/USDT:USDT"),
        ("btcusdt", "BTC-USDT"),
    ],
)
def test_pinned_strategy_matches_the_same_market_spelled_differently(
    pinned, event_symbol
):
    applicable, rejections = _select(pinned, event_symbol)
    assert applicable, (
        f"pinned {pinned!r} must match the same market arriving as "
        f"{event_symbol!r}; got rejections: {rejections}"
    )


# --- the constraint the matcher exists to enforce ---------------------------


def test_pinned_btc_strategy_still_refuses_eth_candles():
    """The whole point of the pin is that it constrains. Canonicalising the
    spelling must not loosen the market."""
    applicable, rejections = _select("BTCUSDT", "ETHUSDT")
    assert not applicable
    assert rejections and "pinned to BTCUSDT" in rejections[0]


def test_pinned_btc_strategy_refuses_sol_even_when_its_own_pool_lists_sol():
    """A pinned symbol wins over the mode pool."""
    applicable, rejections = _select(
        "BTCUSDT", "SOLUSDT", symbols=("BTCUSDT", "SOLUSDT")
    )
    assert not applicable
    assert "pinned to BTCUSDT" in rejections[0]


def test_unrelated_pair_is_not_matched_by_a_shared_quote_asset():
    """BTCUSDT and BTCUSDC are different markets. Canonicalising must not
    collapse everything sharing a base asset."""
    same = TradingController._symbols_refer_to_same_market("BTCUSDT", "BTC-USDC")
    assert same is False


def test_swaps_and_legacy_of_the_same_pair_are_equal():
    assert TradingController._symbols_refer_to_same_market(
        "BTCUSDT", "BTC-USDT-SWAP"
    )
    assert TradingController._symbols_refer_to_same_market("BTCUSDT", "BTCUSDT")
    assert not TradingController._symbols_refer_to_same_market(
        "BTCUSDT", "ETHUSDT"
    )


def test_missing_pins_are_never_the_same_market():
    """Empty/None must not canonicalise into a match."""
    assert TradingController._symbols_refer_to_same_market(None, "BTCUSDT") is False
    assert TradingController._symbols_refer_to_same_market("BTCUSDT", None) is False
    assert TradingController._symbols_refer_to_same_market("", "") is False


def test_a_canonicalisation_failure_falls_back_to_exact_comparison(monkeypatch):
    """If canonicalisation blows up, the matcher must get STRICTER, never wider.

    The production helper imports `_to_legacy_symbol` lazily from CcxtExecutor,
    so that is the seam to break -- not a hook invented for the test.
    """
    from bot_module.exchanges import ccxt_executor

    def _boom(_sym):
        raise RuntimeError("simulated canonicalisation failure")

    monkeypatch.setattr(
        ccxt_executor.CcxtExecutor, "_to_legacy_symbol", staticmethod(_boom)
    )

    # BTCUSDT vs BTC-USDT differ verbatim, so a strict fallback rejects.
    assert (
        TradingController._symbols_refer_to_same_market("BTCUSDT", "BTC-USDT")
        is False
    )
    # An exact match still succeeds, since that never needed canonicalising.
    assert (
        TradingController._symbols_refer_to_same_market("BTCUSDT", "BTCUSDT") is True
    )


# --- timeframe routing must survive -----------------------------------------


def test_timeframe_mismatch_is_still_rejected_after_the_symbol_matches():
    applicable, rejections = _select("BTCUSDT", "BTC-USDT", timeframe="1h")
    assert not applicable
    assert rejections and "timeframe(" in rejections[0]