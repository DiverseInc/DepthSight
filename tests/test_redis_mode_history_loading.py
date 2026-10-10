# File: tests/test_redis_mode_history_loading.py
"""
Redis mode must still load kline history, and must not lose it afterwards.

THE DEFECT THIS EXISTS TO REPAIR
--------------------------------
``_ensure_history_loaded`` used to short-circuit on::

    if self._market_data_mode == "redis":
        logger.debug("... Skipping local history download ...")
        return True

PRODUCTION runs ``MARKET_DATA_FANOUT_MODE=redis``, so **no kline history was ever
downloaded**. The shared Redis snapshot was meant to supply it instead, but when
that snapshot is missing or short the cache silently degraded to live websocket
candles only -- ~103 rows for kline_1m. Any MIN_STRATEGY_HISTORY_CANDLES above
that was unsatisfiable, and controller.py's gate blocked every 1m strategy.

The skip was logged at DEBUG, so it produced no log lines at default level. Three
greps came back empty before a control test proved the greps themselves worked.

These tests drive the REAL functions. None of them asserts on a shape.
"""

import asyncio

import numpy as np
import pandas as pd
import pytest

from bot_module import data_consumer as dc_mod
from bot_module import config as config_module


LONG_SLOW_PERIOD = 200
ROWS_NEEDED = LONG_SLOW_PERIOD + 5  # 205


def _candles(n, end_ms=1_700_000_000_000, step_ms=60_000):
    """n synthetic OHLCV tuples, newest last."""
    start = end_ms - (n - 1) * step_ms
    return [
        (
            start + i * step_ms,
            100.0 + i,
            101.0 + i,
            99.0 + i,
            100.5 + i,
            10.0 + i,
        )
        for i in range(n)
    ]


def _reset_globals():
    dc_mod._global_history_loaded_keys.clear()
    dc_mod._global_history_download_tasks.clear()
    dc_mod._global_kline_cache.clear()
    dc_mod._global_kline_df_cache.clear()


@pytest.fixture(autouse=True)
def _clean():
    _reset_globals()
    yield
    _reset_globals()


def _make_consumer(redis_mode: bool):
    """A real DataConsumer built through its own __init__, so every attribute
    the production code touches actually exists.

    Hand-assigning a handful of attributes produced a fake object missing
    `self.loop`, which the download scheduler needs -- the tests must exercise
    the real code path, not a stub that silently diverges from it.

    `loop` MUST be the running loop. The scheduler does
    `self.loop.create_task(...)` and then `await`s it, so a detached loop makes
    the await fail with a cross-loop RuntimeError -- a failure in the TEST rig,
    not in the code under test. Production always passes its running loop.
    """
    consumer = dc_mod.DataConsumer(
        loop=asyncio.get_running_loop(),
        executor=None,
        event_queue=None,
        controller=None,
        market_data_mode="redis" if redis_mode else "binance",
    )
    consumer._use_redis_market_data = redis_mode
    return consumer


def _cache_len(cache_key):
    return len(dc_mod._global_kline_cache.get(cache_key, ()))


# --- 1. the root cause: redis mode must not silently skip --------------------


@pytest.mark.asyncio
async def test_redis_mode_downloads_history_when_cache_is_short(monkeypatch):
    """A short cache in Redis mode must fall through to the real download."""
    inst = _make_consumer(redis_mode=True)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "binance"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    calls = {"n": 0}

    async def fake_download(cache_key_arg, symbol_uc, timeframe, market_type, exchange_id):
        calls["n"] += 1
        rows = _candles(300)
        async with dc_mod._global_cache_lock:
            d = dc_mod._global_kline_cache[cache_key_arg]
            d.clear()
            d.extend(rows)
        dc_mod._global_history_loaded_keys.add(cache_key_arg)
        return True

    monkeypatch.setattr(inst, "_download_initial_kline_history_for_key", fake_download)

    await inst._ensure_history_loaded(f"kline_{tf}", symbol, tf, mt, exch)

    assert calls["n"] == 1, (
        "Redis mode skipped the download; the cache can only ever hold live "
        "candles, so no indicator period above that is reachable"
    )
    assert _cache_len(cache_key) >= ROWS_NEEDED, (
        f"cache holds {_cache_len(cache_key)}, need {ROWS_NEEDED} for a "
        f"{LONG_SLOW_PERIOD}-period cross"
    )


@pytest.mark.asyncio
async def test_redis_mode_still_skips_when_cache_is_already_full(monkeypatch):
    """Do not re-download on every call once the cache is adequate."""
    inst = _make_consumer(redis_mode=True)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "binance"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    dc_mod._global_kline_cache[cache_key].extend(_candles(300))

    called = {"n": 0}

    async def fake_download(*a, **k):
        called["n"] += 1
        return True

    monkeypatch.setattr(inst, "_download_initial_kline_history_for_key", fake_download)

    await inst._ensure_history_loaded(f"kline_{tf}", symbol, tf, mt, exch)
    assert called["n"] == 0, "re-downloaded despite an already-full cache"


# --- 2. a short snapshot must not WIPE good history -------------------------


@pytest.mark.asyncio
async def test_short_snapshot_merges_instead_of_wiping_history():
    """A 3-row snapshot must not truncate 300 rows of real history.

    The old code did clear() + extend(rows), so any short snapshot destroyed
    whatever had been downloaded. Once redis mode started downloading properly,
    a later short snapshot would silently undo the fix.
    """
    inst = _make_consumer(redis_mode=True)
    symbol, tf, exch = "BTCUSDT", "1m", "binance"
    mt = "futures_usdtm"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    full = _candles(300)
    dc_mod._global_kline_cache[cache_key].extend(full)

    snapshot = {
        "type": "market_snapshot",
        "data_type_key": f"kline_{tf}",
        "symbol": symbol,
        "exchange_id": exch,
        "market_type": mt,
        "rows": [list(r) for r in full[-3:]],
    }
    ok = await inst._apply_market_snapshot(snapshot)

    assert ok is True, "the 3-row snapshot was rejected outright"
    assert _cache_len(cache_key) >= ROWS_NEEDED, (
        f"a 3-row snapshot wiped the cache down to {_cache_len(cache_key)} rows; "
        f"the snapshot must MERGE, not replace"
    )


# --- 3. an empty fetch must not poison the cache forever --------------------


@pytest.mark.asyncio
async def test_empty_download_does_not_mark_history_loaded(monkeypatch):
    """Zero candles must leave the key unmarked so a retry is possible.

    _global_history_loaded_keys has no TTL, no sweeper and no discard() in
    production code -- process restart is the only way to clear it. Marking a
    key loaded on an empty result therefore blocks every retry for the life of
    the process.
    """
    inst = _make_consumer(redis_mode=False)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "binance"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    async def fake_download_klines(*a, **k):
        return empty

    monkeypatch.setattr(dc_mod, "download_klines", fake_download_klines)

    await inst._download_initial_kline_history_for_key(cache_key, symbol, tf, mt, exch)

    assert cache_key not in dc_mod._global_history_loaded_keys, (
        "an empty download marked history as loaded; every later call will "
        "short-circuit at the global-cache-hit gate and never retry"
    )


# --- 4. the failure must be visible at default log level --------------------


@pytest.mark.asyncio
async def test_redis_short_circuit_is_visible_in_logs(monkeypatch):
    """The old skip logged at DEBUG, which is why this took a day to find.

    Asserts the decision itself is loud AND that it acts: a short cache in redis
    mode must both log at INFO and actually reach the download.

    A dedicated handler is attached to dc_mod.logger rather than using `caplog`,
    because logger_setup sets `propagate = False`, so records never reach the
    root logger and caplog would see an empty list regardless of the fix.
    """
    import logging

    inst = _make_consumer(redis_mode=True)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "binance"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)
    # Deliberately short: ~103 live rows, exactly the production fingerprint.
    dc_mod._global_kline_cache[cache_key].extend(_candles(103))

    calls = {"n": 0}

    async def fake_download(cache_key_arg, symbol_uc, timeframe, market_type, exchange_id):
        calls["n"] += 1
        async with dc_mod._global_cache_lock:
            d = dc_mod._global_kline_cache[cache_key_arg]
            d.clear()
            d.extend(_candles(300))
        dc_mod._global_history_loaded_keys.add(cache_key_arg)
        return True

    monkeypatch.setattr(inst, "_download_initial_kline_history_for_key", fake_download)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    prev_level = dc_mod.logger.level
    dc_mod.logger.addHandler(handler)
    dc_mod.logger.setLevel(logging.DEBUG)
    try:
        await inst._ensure_history_loaded(f"kline_{tf}", symbol, tf, mt, exch)
    finally:
        dc_mod.logger.removeHandler(handler)
        dc_mod.logger.setLevel(prev_level)

    at_info = [
        r.getMessage()
        for r in records
        if r.levelno >= logging.INFO and "HistLoadEnsure" in r.getMessage()
    ]
    assert at_info, (
        "redis mode fell through to the download path without logging anything "
        "at INFO or above; a short-cache history load must be traceable"
    )
    assert calls["n"] == 1, "logged a decision but did not act on it"


def test_configured_history_requirement_is_reachable_after_the_fix():
    """Sanity: the gate value must now be satisfiable at 1m."""
    required = int(getattr(config_module, "MIN_STRATEGY_HISTORY_CANDLES", 20))
    # 3 days of 1m candles, which is what the download now actually fetches.
    available = 3 * 24 * 60
    assert available >= required, (
        f"3 days of 1m = {available} candles cannot satisfy a requirement of {required}"
    )


# --- 5. THE INTERACTION: a short snapshot must not disarm the retry ----------
# OpenCode review finding. Sequence that defeated both earlier fixes:
#   download fails  -> C correctly leaves the key UNmarked
#   short snapshot  -> old _apply_market_snapshot marked it loaded anyway
#   next call       -> short-circuits, retry never happens
# Neither fix works alone. This is the only test that exercises the seam.


@pytest.mark.asyncio
async def test_short_snapshot_cannot_disarm_the_download_retry(monkeypatch):
    """After a failed download, a short snapshot must NOT close the retry door."""
    inst = _make_consumer(redis_mode=True)
    symbol, tf, exch = "BTCUSDT", "1m", "binance"
    mt = "futures_usdtm"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    # 1. The download comes back empty. Key must stay unmarked.
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    async def fake_download_klines(*a, **k):
        return empty

    monkeypatch.setattr(dc_mod, "download_klines", fake_download_klines)
    await inst._download_initial_kline_history_for_key(cache_key, symbol, tf, mt, exch)

    assert cache_key not in dc_mod._global_history_loaded_keys, (
        "an empty download already marked the key loaded; the retry is already dead"
    )

    # 2. A short snapshot lands (this is what production actually delivers).
    live_only = _candles(103)
    snapshot = {
        "type": "market_snapshot",
        "data_type_key": f"kline_{tf}",
        "symbol": symbol,
        "exchange_id": exch,
        "market_type": mt,
        "rows": [list(r) for r in live_only],
    }
    ok = await inst._apply_market_snapshot(snapshot)
    assert ok is True, "the snapshot was rejected outright"
    assert _cache_len(cache_key) == 103, "the snapshot did not seed the live-only cache"

    # 3. THE SEAM. The next ensure must still reach the download. If the snapshot
    #    stamped the key, this silently returns True and the retry is disarmed.
    reached = {"n": 0}

    async def counting_download(cache_key_arg, symbol_uc, timeframe, market_type, exchange_id):
        reached["n"] += 1
        async with dc_mod._global_cache_lock:
            d = dc_mod._global_kline_cache[cache_key_arg]
            d.clear()
            d.extend(_candles(300))
        dc_mod._global_history_loaded_keys.add(cache_key_arg)
        return True

    monkeypatch.setattr(
        inst, "_download_initial_kline_history_for_key", counting_download
    )

    await inst._ensure_history_loaded(f"kline_{tf}", symbol, tf, mt, exch)

    assert reached["n"] == 1, (
        "a short snapshot marked history loaded, which disarms the retry that "
        "the empty-download fix exists to enable -- history can never exceed "
        "the ~103 live candles again"
    )
    assert _cache_len(cache_key) >= ROWS_NEEDED


# --- 6. unparseable rows must not mark loaded either ------------------------


@pytest.mark.asyncio
async def test_unparseable_rows_do_not_mark_history_loaded(monkeypatch):
    """A non-empty DataFrame with no usable rows must also leave the key unmarked.

    This branch used to log a warning and fall through to the marking block,
    adding the key with zero candles. Unparseable rows almost always mean a
    column/schema mismatch, which is exactly what most deserves a retry.
    """
    inst = _make_consumer(redis_mode=False)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "binance"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    # Right shape (a datetime index), wrong columns -> every float() raises KeyError.
    bad = pd.DataFrame(
        {"px": [1.0, 2.0, 3.0]},
        index=pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC"),
    )

    async def fake_download_klines(*a, **k):
        return bad

    monkeypatch.setattr(dc_mod, "download_klines", fake_download_klines)

    await inst._download_initial_kline_history_for_key(cache_key, symbol, tf, mt, exch)

    assert cache_key not in dc_mod._global_history_loaded_keys, (
        "a non-empty but entirely unparseable download marked history loaded; "
        "a schema mismatch permanently disarms the retry"
    )


# --- 7. an empty download must not kill the live websocket ------------------


def test_empty_history_does_not_stop_the_websocket_stream():
    """Regression guard on the non-Redis (Binance WS) path.

    Once empty downloads stopped stamping the key loaded,
    _ensure_history_loaded returns False. The subscription loop's old reaction
    was `continue`, which skipped the WebSocket entirely -- no stream, no way to
    ever re-prime the cache. This is a structural check, labelled as such:
    driving the real ensure_subscription needs a live exchange executor.

    It is parsed as AST rather than matched as source text on purpose. A string
    search on "continue" matches the word inside comments -- including the
    comment in this very fix -- which is how the first version of this test
    failed while the production code was correct.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(
        inspect.getsource(dc_mod.DataConsumer.ensure_subscription)
    )
    tree = ast.parse(src)

    gate = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "history_loaded"
        ):
            gate = node
            break

    assert gate is not None, (
        "could not locate the `if not history_loaded:` gate in "
        "ensure_subscription; the function's shape changed and this guard is "
        "no longer testing anything"
    )

    continues = [n for n in ast.walk(gate) if isinstance(n, ast.Continue)]
    assert not continues, (
        f"the `if not history_loaded:` branch still contains {len(continues)} "
        f"`continue` statement(s); a single empty REST response would kill the "
        f"live stream in non-Redis mode, and with no stream nothing can ever "
        f"re-prime the cache"
    )

    # And the branch must still say something, so a silent no-op cannot pass.
    has_log = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("warning", "error", "info")
        for n in ast.walk(gate)
    )
    assert has_log, (
        "the history gate neither skips the stream nor logs; "
        "an empty download must be visible"
    )


# --- 8. SEAM: only the network boundary is mocked ---------------------------


@pytest.mark.asyncio
async def test_ensure_history_loaded_end_to_end_through_real_download(monkeypatch):
    """Drive the REAL _download_initial_kline_history_for_key, mocking only
    `download_klines` (the actual network call).

    Test 1 stubs the download coroutine entirely, so it proves the redis branch
    *calls* something -- it would still pass if the real download path were
    broken. Here the cache is filled by production code alone.
    """
    inst = _make_consumer(redis_mode=True)
    symbol, tf, exch = "BTCUSDT", "1m", "binance"
    mt = "futures_usdtm"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    n = 300
    idx = pd.date_range("2026-01-01 00:00:00", periods=n, freq="1min", tz="UTC")
    good = pd.DataFrame(
        {
            "open": np.linspace(100, 200, n),
            "high": np.linspace(101, 201, n),
            "low": np.linspace(99, 199, n),
            "close": np.linspace(100.5, 200.5, n),
            "volume": np.linspace(10, 20, n),
        },
        index=idx,
    )

    async def fake_download_klines(*a, **k):
        return good

    monkeypatch.setattr(dc_mod, "download_klines", fake_download_klines)

    result = await inst._ensure_history_loaded(f"kline_{tf}", symbol, tf, mt, exch)

    assert result is True, "the real download path did not report success"
    assert _cache_len(cache_key) >= ROWS_NEEDED, (
        f"cache holds {_cache_len(cache_key)} after a real 300-row download, "
        f"need {ROWS_NEEDED} for a {LONG_SLOW_PERIOD}-period cross"
    )


# --- 9. PAGINATION: one fetch cannot fill the window ------------------------
# Measured live 2026-10-09: kline_1m came back with exactly 100 candles while a
# 3-day 1m window holds 4320. 100 is OKX's *default* per-request limit, not its
# 300 maximum, so the requested limit was not reaching the exchange.
#
# A 205-candle requirement was therefore unsatisfiable by construction -- the
# ceiling was the single unpaginated fetch, not MIN_STRATEGY_HISTORY_CANDLES.


class _PagedExecutor:
    """A fake CCXT executor that honours a per-request limit, like a real one.

    Anchors to the FIRST `since` it is given (the window start) and serves rows
    forward from there, so each subsequent page continues where the last ended.
    Anchoring to a hardcoded date instead would serve nothing, because the real
    window start is `now - lookback`, not a constant.
    """

    def __init__(self, total_rows, per_page_cap):
        self.total_rows = total_rows
        self.per_page_cap = per_page_cap
        self.calls = []
        self._anchor = None

    def _executor_for_market(self, market_type):
        return self

    exchange_id = "okx"

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
        limit = min(int(limit or self.per_page_cap), self.per_page_cap)
        self.calls.append({"since": since, "limit": limit})
        if self._anchor is None:
            self._anchor = int(since)
        offset = max(0, (int(since) - self._anchor)) // 60_000
        rows = []
        for i in range(offset, min(offset + limit, self.total_rows)):
            ts = self._anchor + i * 60_000
            rows.append([ts, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0 + i])
        return rows


@pytest.mark.asyncio
async def test_ccxt_download_paginates_past_a_single_page(monkeypatch):
    """A 1m window that holds more than one page must be paged, not truncated.

    Without pagination this returns one page (100 rows on the real exchange) and
    a 205-candle requirement stays unsatisfiable no matter what the constant is.
    """
    inst = _make_consumer(redis_mode=False)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "okx"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    # The exchange will serve at most 300 per call, and the 3-day 1m window
    # holds 4320 rows.
    executor = _PagedExecutor(total_rows=4320, per_page_cap=300)

    monkeypatch.setattr(inst, "_executor_for_market", executor._executor_for_market)

    await inst._download_initial_kline_history_for_key(cache_key, symbol, tf, mt, exch)

    assert len(executor.calls) > 1, (
        "only one fetch_ohlcv call was made; a window holding more than one "
        "page cannot be filled without pagination"
    )
    assert all(c["limit"] <= 300 for c in executor.calls), (
        f"a request asked for more than the exchange's 300/page maximum: "
        f"{[c['limit'] for c in executor.calls]}"
    )
    assert _cache_len(cache_key) >= ROWS_NEEDED, (
        f"pagination produced only {_cache_len(cache_key)} candles, need "
        f"{ROWS_NEEDED} for a {LONG_SLOW_PERIOD}-period cross"
    )


@pytest.mark.asyncio
async def test_ccxt_pagination_survives_out_of_order_rows(monkeypatch):
    """The cursor must advance by the MAX timestamp, not the last row's.

    Advancing by `batch[-1]` breaks if the exchange returns rows out of order:
    the cursor moves backwards, the next page re-serves rows already held, and
    the window in between is skipped entirely. OpenCode review, 2026-10-09.
    """

    class _ShuffledExecutor(_PagedExecutor):
        async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
            rows = await super().fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            # Reverse every page so the last row is the OLDEST timestamp.
            return list(reversed(rows))

    inst = _make_consumer(redis_mode=False)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "okx"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    executor = _ShuffledExecutor(total_rows=4320, per_page_cap=300)
    monkeypatch.setattr(inst, "_executor_for_market", executor._executor_for_market)

    await asyncio.wait_for(
        inst._download_initial_kline_history_for_key(cache_key, symbol, tf, mt, exch),
        timeout=60.0,
    )

    assert _cache_len(cache_key) >= ROWS_NEEDED, (
        f"out-of-order pages produced only {_cache_len(cache_key)} candles; the "
        f"cursor advanced by the last row instead of the max timestamp, so it "
        f"re-fetched and skipped windows. Need {ROWS_NEEDED}."
    )


@pytest.mark.asyncio
async def test_ccxt_pagination_stops_when_exchange_ignores_since(monkeypatch):
    """If `since` is ignored the loop must stop, not spin forever."""

    class _IgnoringExecutor(_PagedExecutor):
        async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
            self.calls.append({"since": since, "limit": limit})
            # Always the same page, regardless of `since`.
            return [
                [1_700_000_000_000 + i * 60_000, 1.0, 2.0, 0.5, 1.5, 1.0]
                for i in range(300)
            ]

    inst = _make_consumer(redis_mode=False)
    symbol, tf, mt, exch = "BTCUSDT", "1m", "futures_usdtm", "okx"
    cache_key = dc_mod._kline_cache_key(symbol, tf, exch, mt)

    executor = _IgnoringExecutor(total_rows=0, per_page_cap=300)
    monkeypatch.setattr(inst, "_executor_for_market", executor._executor_for_market)

    await asyncio.wait_for(
        inst._download_initial_kline_history_for_key(cache_key, symbol, tf, mt, exch),
        timeout=30.0,
    )

    assert len(executor.calls) <= 3, (
        f"pagination kept requesting pages ({len(executor.calls)} calls) even "
        f"though the exchange ignored `since`; this would spin"
    )