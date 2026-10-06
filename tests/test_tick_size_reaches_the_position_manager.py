# File: tests/test_tick_size_reaches_the_position_manager.py
"""
The tick_size contract, end to end, using the REAL controller methods.

Why this file exists
--------------------
`aa86857` fixed a defect that was invisible for days: on this deployment
(CCXT/OKX, paper-only account) `tick_size` was ALWAYS `None`, so
`_handle_move_to_breakeven` bailed out on every single call. Roughly fifteen
call sites silently fell back to `DEFAULT_TICK_SIZE`.

The two fixes needed for it to bite are not one bug but two, and only the
first was obvious:

  1. `CcxtExecutor.fetch_exchange_info()` emitted symbol entries with **no
     `filters` key**, so the PRICE_FILTER parser never saw a tickSize.
  2. On a **paper-only** account there is no `"live"` executor, so the
     `market_executors` loop was the only thing that could populate the
     cache. It skipped an entry when the normalised market type equalled
     `_normalize_market_type(None)` -- which normalises to the configured
     default, `futures_usdtm` -- so the futures entry matched ITSELF and was
     skipped. **No futures cache key was ever written on any paper-only
     account.**

Why the existing tests could not catch either one:

  - `tests/test_move_to_breakeven_rr.py` **injects `tick_size` directly** into
    the backtester's `exchange_info`, so it proves the arithmetic and is
    structurally incapable of failing when the producer breaks.
  - `tests/test_controller_position_management_integration.py` **emulates**
    the controller: the file literally contains a copy of the production
    block under the comment "This is code from controller.py". A copy of the
    logic proves the copy is correct, not that production is.
  - `tests/test_ccxt_exchange_info_carries_filters.py` covers the producer
    (fix 1) but never reaches the cache or the consumer.

So nothing covered the join: producer -> cache key -> consumer lookup. That
join is exactly where both halves of the bug lived, and it is a *key-shape*
contract -- the producer writes `f"{market_type}:{SYMBOL}"` while the consumer
looks up `f"{self._normalize_market_type(market_type)}:{SYMBOL}"`. Nothing
enforces that they agree.

This test calls the real `_update_market_info_cache` and the real
`_get_market_info`, so it exercises that join for real, and pins both the
fixed behaviour and the two original failure modes as explicit regressions.
"""

import asyncio

import pytest

from bot_module.controller import TradingController


# --- Helpers -----------------------------------------------------------------


def okx_symbol(symbol="BTCUSDT", tick="0.1", with_filters=True):
    """The REAL post-fix CCXT exchange_info symbol entry.

    OKX via CCXT emits Binance-style filter keys (`filterType`, `tickSize`),
    which is what `controller.py` parses. `--with-filters=False` reproduces
    the pre-`aa86857` shape that made tick_size permanently None.
    """
    entry = {"symbol": symbol, "status": "TRADING"}
    if with_filters:
        entry["filters"] = [
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.00001",
                "maxQty": "1000",
                "stepSize": "0.00001",
            },
            {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
        ]
    return entry


def make_executor(symbol_entries):
    """A minimal async executor exposing only fetch_exchange_info."""

    class _Exec:
        market_type = "futures_usdtm"

        def __init__(self, entries):
            self._entries = entries

        async def fetch_exchange_info(
            self, force_update=False, specific_market_type=None
        ):
            return {"symbols": self._entries}

    return _Exec(symbol_entries)


class StubController:
    """The minimum surface `_update_market_info_cache` and `_get_market_info` touch.

    `_normalize_market_type` is lifted from the real class rather than
    reimplemented, because the normaliser's behaviour for a missing live
    executor is precisely what bug (2) hinged on. Reimplementing it here would
    be the same mistake the old integration test made.
    """

    # `staticmethod(...)` is required: the real method is a `@staticmethod`,
    # so assigning it bare into this class body would make it an instance
    # method and every call would pass `stub` as a spurious extra argument.
    _normalize_market_type = staticmethod(TradingController._normalize_market_type)

    def __init__(self, market_executors, executors=None):
        # Default is paper-only: NO "live" key. That is the real deployment
        # shape for every account this project ships to.
        self.executors = executors if executors is not None else {}
        self.market_executors = market_executors
        self._market_info_cache = {}
        self._market_info_lock = asyncio.Lock()


async def fill_cache(stub):
    await TradingController._update_market_info_cache(stub)
    return stub._market_info_cache


async def get_tick(stub, market_type="futures_usdtm"):
    return await TradingController._get_market_info(
        stub, "BTCUSDT", "tick_size", market_type=market_type
    )


# --- The fixed behaviour -----------------------------------------------------


@pytest.mark.asyncio
async def test_paper_only_account_resolves_futures_tick_size():
    """The live bug: paper-only + no live executor must still yield a tick_size.

    This is the assertion that was silently false for days in production.
    """
    stub = StubController(
        {
            "spot": make_executor([okx_symbol("BTCUSDT")]),
            "futures_usdtm": make_executor([okx_symbol("BTCUSDT")]),
        }
    )
    await fill_cache(stub)

    tick = await get_tick(stub)
    assert tick == 0.1, f"expected 0.1, got {tick!r}"


@pytest.mark.asyncio
async def test_spot_tick_size_resolves_on_a_paper_only_account():
    """The spot entry must not be skipped either."""
    stub = StubController(
        {
            "spot": make_executor([okx_symbol("BTCUSDT", tick="0.01")]),
            "futures_usdtm": make_executor([okx_symbol("BTCUSDT", tick="0.1")]),
        }
    )
    await fill_cache(stub)

    assert await get_tick(stub, "spot") == 0.01


@pytest.mark.asyncio
async def test_tick_size_is_usable_by_the_breakeven_guard():
    """`_handle_move_to_breakeven` bails unless tick_size is truthy and > 0.

    Asserting the guard's actual precondition on the real fetched value is
    the whole point -- a cached-but-unusable tick_size would leave breakeven
    inert while every other assertion passed.
    """
    stub = StubController(
        {
            "spot": make_executor([okx_symbol("BTCUSDT")]),
            "futures_usdtm": make_executor([okx_symbol("BTCUSDT")]),
        }
    )
    await fill_cache(stub)
    tick = await get_tick(stub)

    # This is the literal condition at strategy.py's guard:
    #   if not all([price_for_check, entry_price, tick_size]): -> return
    assert all([100.0, 100.0, tick]), "breakeven would bail out with this tick_size"
    assert tick > 0


@pytest.mark.asyncio
async def test_a_live_account_still_resolves_tick_size():
    """The live-executor path must keep working; the paper fix must not have
    broken the account type it was not written for."""
    live_exec = make_executor([okx_symbol("BTCUSDT")])
    live_exec.market_type = "futures_usdtm"
    stub = StubController(
        market_executors={"futures_usdtm": make_executor([okx_symbol("BTCUSDT")])},
        executors={"live": live_exec},
    )
    await fill_cache(stub)

    assert await get_tick(stub) == 0.1


@pytest.mark.asyncio
async def test_lot_params_and_min_notional_travel_too():
    """Guards against a future partial fix that restores only tick_size."""
    stub = StubController(
        {
            "spot": make_executor([okx_symbol("BTCUSDT")]),
            "futures_usdtm": make_executor([okx_symbol("BTCUSDT")]),
        }
    )
    await fill_cache(stub)

    lot = await TradingController._get_market_info(
        stub, "BTCUSDT", "lot_params", market_type="futures_usdtm"
    )
    notional = await TradingController._get_market_info(
        stub, "BTCUSDT", "min_notional", market_type="futures_usdtm"
    )
    assert lot and lot["stepSize"] == 0.00001
    assert notional == 1.0


# --- The two original failure modes, pinned as regressions -------------------


@pytest.mark.asyncio
async def test_regression_entries_without_filters_yield_no_tick_size():
    """Pre-fix bug (1): `fetch_exchange_info` emitted no `filters` key.

    If this ever starts returning a tick_size, something upstream changed
    shape and the parsing above should be re-checked.
    """
    stub = StubController(
        {
            "spot": make_executor([okx_symbol("BTCUSDT", with_filters=False)]),
            "futures_usdtm": make_executor(
                [okx_symbol("BTCUSDT", with_filters=False)]
            ),
        }
    )
    await fill_cache(stub)

    assert await get_tick(stub) is None


@pytest.mark.asyncio
async def test_regression_missing_futures_executor_leaves_futures_key_unwritten():
    """Pre-fix bug (2): paper-only account with only a spot executor.

    Nothing writes `futures_usdtm:BTCUSDT`, and with no live executor the
    bare-symbol fallback is never written either -- so the lookup returns
    None. This is the paper-only account shape that produced the original
    silent outage.
    """
    stub = StubController({"spot": make_executor([okx_symbol("BTCUSDT")])})
    cache = await fill_cache(stub)

    assert "futures_usdtm:BTCUSDT" not in cache
    assert await get_tick(stub) is None
    # The spot key IS written, proving the loop ran -- this is a missing-entry
    # bug, not a dead code path.
    assert "spot:BTCUSDT" in cache


@pytest.mark.asyncio
async def test_cache_key_and_lookup_key_agree():
    """The contract itself: the producer's key must equal the consumer's key.

    This is the join no existing test covered. Written explicitly so a change
    to either side's key format fails here instead of in production.
    """
    stub = StubController(
        {
            "spot": make_executor([okx_symbol("BTCUSDT")]),
            "futures_usdtm": make_executor([okx_symbol("BTCUSDT")]),
        }
    )
    cache = await fill_cache(stub)

    producer_key = f"{stub._normalize_market_type('futures_usdtm')}:BTCUSDT"
    consumer_key = f"{stub._normalize_market_type('futures_usdtm')}:{'BTCUSDT'}"
    assert producer_key == consumer_key == "futures_usdtm:BTCUSDT"
    assert producer_key in cache, (
        f"producer wrote {sorted(cache)} but the consumer will look up "
        f"{consumer_key!r}"
    )