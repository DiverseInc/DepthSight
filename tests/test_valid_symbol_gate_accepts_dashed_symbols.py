# tests/test_valid_symbol_gate_accepts_dashed_symbols.py
"""
The valid-symbols gate rejected exchange-native symbol forms.

Live evidence, 2026-10-14:

    GET /api/v1/market-data/candle-health  ->  3 streams, ALL ETHUSDT
    GET /api/v1/strategies?mode=paper      ->  be485ab9 running, symbol "BTC-USDT"

`ensure_subscription` compares the requested symbol against the set built by
`_get_valid_symbols_from_exchange_info`, which `fetch_exchange_info` keys by
LEGACY symbol (`base + "USDT"`, plus a hardcoded top-volume safety net):

    valid_symbols = {"BTCUSDT", "ETHUSDT", ...}

The comparison was `if uc_symbol not in valid_symbols`, verbatim. So:

    "ETHUSDT"   in valid_symbols  -> subscribes   (why ETH kept flowing)
    "BTC-USDT"  not in valid_symbols -> "Subscription SKIPPED" -> ZERO candles

while the strategy still reported `running`, with no error surfaced anywhere.

Why 189600b could not have fixed this: it patched `_normalize_symbol`, which is
applied much later, inside `_ccxt_pro_data_ws_loop` right before `watch_ohlcv`.
The gate above `continue`s before that code is ever reached. Deploying HEAD does
not fix a dashed config.

These tests drive the REAL `ensure_subscription` and assert on the streams it
actually creates. A shape assertion on the gate would not catch a gate that
compares the wrong two forms.
"""

import asyncio

import pytest

from bot_module import config as global_config
from bot_module import data_consumer as dc_mod
from bot_module.data_consumer import DataConsumer
from bot_module.exchanges.ccxt_executor import CcxtExecutor


# What fetch_exchange_info actually returns: legacy, concatenated, uppercase.
LEGACY_VALID_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"}


class _StubExecutor:
    """Minimal executor. Carries the REAL canonicaliser under test."""

    exchange_id = "okx"
    sandbox = False
    api_key = None
    # Bind the production implementation rather than a re-implementation, so a
    # regression in _to_legacy_symbol is caught here too.
    _to_legacy_symbol = staticmethod(CcxtExecutor._to_legacy_symbol)


def _make_consumer() -> DataConsumer:
    consumer = DataConsumer(
        loop=asyncio.get_event_loop(),
        executor=_StubExecutor(),
        market_data_mode="direct",
    )
    consumer._running = True
    return consumer


def _task_keys(consumer: DataConsumer):
    return set(consumer._binance_market_data_ws_tasks.keys())


def _key_for(consumer: DataConsumer, symbol: str) -> str:
    """Mirror the key ensure_subscription builds, so we assert on the real one.

    Note `ensure_subscription` lowercases WITHOUT stripping, so a padded symbol
    yields a key containing spaces. Mirror that exactly rather than tidying it.
    """
    return f"okx:futures_usdtm:{symbol.lower()}@kline_1m"


@pytest.fixture(autouse=True)
def _clean_registries():
    """The WS registry is module-global; a leftover entry hides a real failure."""
    dc_mod._global_ws_registry.clear()
    dc_mod._global_event_queues.clear()
    yield
    for entry in list(dc_mod._global_ws_registry.values()):
        task = entry.get("task")
        if task is not None and not task.done():
            task.cancel()
    dc_mod._global_ws_registry.clear()
    dc_mod._global_event_queues.clear()


def _wire(consumer: DataConsumer):
    """Stub only the I/O. The gate under test is NOT stubbed."""
    consumer.set_market_executors({"futures_usdtm": consumer._executor})

    async def _valid_symbols(market_type, force_update=False):
        return set(LEGACY_VALID_SYMBOLS)

    async def _history_loaded(*args, **kwargs):
        return True

    async def _ws_loop(*args, **kwargs):
        await asyncio.sleep(3600)

    consumer._get_valid_symbols_from_exchange_info = _valid_symbols
    consumer._ensure_history_loaded = _history_loaded
    consumer._ccxt_pro_data_ws_loop = _ws_loop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    [
        "BTC-USDT",  # OKX native instrument id -- the live failing config
        "btc-usdt",  # lowercase dashed
        " BTC-USDT ",  # stray whitespace
        "BTC-USDT-SWAP",  # OKX perpetual native id
        "BTC/USDT:USDT",  # already CCXT-unified
    ],
)
async def test_dashed_and_native_forms_subscribe(symbol):
    """The exact failure: every form of BTC that reaches this gate must subscribe."""
    global_config.TRADING_MARKET_TYPE = "futures_usdtm"
    consumer = _make_consumer()
    _wire(consumer)

    await consumer.ensure_subscription(
        "kline_1m", symbol, market_type="futures_usdtm"
    )
    await asyncio.sleep(0.05)

    keys = _task_keys(consumer)
    assert keys, (
        f"no stream created for {symbol!r}: the valid-symbols gate rejected it "
        f"verbatim against the legacy-keyed set {sorted(LEGACY_VALID_SYMBOLS)}"
    )
    assert _key_for(consumer, symbol) in keys


@pytest.mark.asyncio
async def test_legacy_symbol_still_subscribes():
    """No regression: the plain form that always worked must still work."""
    global_config.TRADING_MARKET_TYPE = "futures_usdtm"
    consumer = _make_consumer()
    _wire(consumer)

    await consumer.ensure_subscription(
        "kline_1m", "ETHUSDT", market_type="futures_usdtm"
    )
    await asyncio.sleep(0.05)

    assert _key_for(consumer, "ETHUSDT") in _task_keys(consumer)


@pytest.mark.asyncio
async def test_genuinely_unknown_symbol_is_still_rejected():
    """The fix must not turn the gate into a blanket accept."""
    global_config.TRADING_MARKET_TYPE = "futures_usdtm"
    consumer = _make_consumer()
    _wire(consumer)

    await consumer.ensure_subscription(
        "kline_1m", "NOTAREALCOIN-USDT", market_type="futures_usdtm"
    )
    await asyncio.sleep(0.05)

    assert not _task_keys(consumer)


@pytest.mark.asyncio
async def test_base_asset_alone_is_not_enough():
    """`_to_legacy_symbol("BTC-USDT")` is "BTCUSDT", never just "BTC".

    A regression that strips the quote would make every base asset look valid
    and silently subscribe to unsellable markets.
    """
    consumer = _make_consumer()
    stub = _StubExecutor()
    assert stub._to_legacy_symbol("BTC-USDT") == "BTCUSDT"
    assert stub._to_legacy_symbol("BTC-USDT-SWAP") == "BTCUSDT"
    assert stub._to_legacy_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert "BTC" not in LEGACY_VALID_SYMBOLS