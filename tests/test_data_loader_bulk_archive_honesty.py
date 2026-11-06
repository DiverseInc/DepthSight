# tests/test_data_loader_bulk_archive_honesty.py
"""The bulk-archive fallback must never let a caller believe it got the
data it asked for when it did not.

Every defect covered here is the SAME failure in different clothes: the
system proceeds with data that is different from, or shorter than, what was
requested.  One section per defect:

* D1  the archive frame is NOT the REST frame (closed bars only, a trailing
     edge short by the publication lag, its own bar grid) and the difference
     is part of the API the caller is given -- ENFORCED, not logged;
* D2  the leading-edge shortfall is ENFORCED: a frame that is shorter than
     the requested window at the leading edge is refused unless the archive
     can PROVE the symbol did not trade then;
* D3  a load with a usable local parquet cache and no remote data COMPLETES
     instead of returning None;
* D4  the archive deadline is SCALED to the window, and a genuinely missing
     daily file is served and NAMED instead of killing the load;

plus the two source guards: a 451 that did not come from Binance does not
trigger the fallback, and a COIN-M request is never answered with USDT-M
archive data.

NO NETWORK IS USED HERE.  The archives are built locally as real ``.zip``
files containing a real CSV -- through the SAME helpers the rest of the
suite uses -- and the HTTP fetch is stubbed.  Where a REAL committed
Binance archive can answer the question, it is used instead of a synthetic
one.
"""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from bot_module import bulk_archive, config, data_loader

# The fixture builders of the main suite are reused: a synthetic archive
# that does not look like Binance's would be a fixture that can invent a
# defect, which has already cost this project one false blocker.
from tests.test_data_loader_bulk_archive import (  # noqa: F401
    FIXTURE_DIR,
    FakeArchiveServer,
    _daily_url,
    _fixture,
    _make_rows,
    _make_zip,
    _monthly_url,
    _rows_to_csv,
    archive_server,
)

FUTURES = "futures_usdtm"
SPOT = "spot"
COINM = "futures_coinm"

# The REAL body Binance answers a geo-blocked request with.  MEASURED on
# 2026-11-02 against the live blocked endpoint:
#
#   {"code": 0, "msg": "Service unavailable from a restricted location
#    according to 'b. Eligibility' in https://www.binance.com/en/terms.
#    Please contact customer service if you believe you received this
#    message in error."}
BINANCE_GEO_BLOCK_BODY = (
    '{"code":0,"msg":"Service unavailable from a restricted location according '
    "to 'b. Eligibility' in https://www.binance.com/en/terms. Please contact "
    'customer service if you believe you received this message in error."}'
)

# The body a corporate proxy or a WAF answers a 451 with.  HTTP 451 is
# "Unavailable For Legal Reasons" (RFC 7725) and proxies use it for their
# OWN reasons -- this one has nothing to do with Binance.
PROXY_GEO_BLOCK_BODY = (
    "<html><head><title>451 Unavailable For Legal Reasons</title></head>"
    "<body>Access to this host has been denied by the corporate proxy. "
    "Contact helpdesk@example.com for an exception.</body></html>"
)


def _http_error_with_body(status_code: int, body: str, url: str):
    """An HTTPError carrying a response with a real body."""
    response = requests.Response()
    response.status_code = status_code
    response.reason = "Unavailable For Legal Reasons"
    response.url = url
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return requests.exceptions.HTTPError(
        f"{status_code} Client Error for url: {url}", response=response
    )


def _binance_blocked_rest_api(*args, **kwargs):
    """The measured Binance geo-block: HTTP 451 with Binance's own body."""
    raise _http_error_with_body(
        451, BINANCE_GEO_BLOCK_BODY, "https://api.binance.com/api/v3/klines"
    )


def _proxy_blocked_rest_api(*args, **kwargs):
    """A PROXY's 451: the same status code, a body that is not Binance's."""
    raise _http_error_with_body(
        451, PROXY_GEO_BLOCK_BODY, "https://api.binance.com/api/v3/klines"
    )


def _frozen_clock(moment: datetime):
    """A datetime subclass whose ``now()`` is pinned to ``moment``."""

    class _Frozen(datetime):
        FROZEN = moment

        @classmethod
        def now(cls, tz=None):
            return cls.FROZEN.astimezone(tz) if tz else cls.FROZEN

    return _Frozen


@pytest.fixture
def frozen_clock(monkeypatch):
    """Installs a pinned clock into the archive module and returns a setter."""

    def _set(moment: datetime):
        monkeypatch.setattr(bulk_archive, "datetime", _frozen_clock(moment))
        return moment

    return _set


class _FakeArchiveResponse:
    """A 200 answer carrying a real archive payload."""

    def __init__(self, content, status_code=200):
        self.status_code = status_code
        self.content = content
        self.reason = "OK"

    def raise_for_status(self):
        return None


class _SlowArchiveSession:
    """An archive host that answers every file, but slowly.

    Used to show that the budget of a load is SCALED to the window: with a
    fixed budget a window of many months runs out of time half way through
    it, on a link that is answering perfectly well.
    """

    def __init__(self, served, delay=0.02):
        self.served = served
        self.delay = delay
        self.requests = []

    def get(self, url, params=None, timeout=None):
        import time

        self.requests.append(url)
        time.sleep(self.delay)
        if timeout is not None and self.delay > timeout:
            # The real session raises when the host does not answer inside
            # the timeout it was given.  A fake that ignored the timeout
            # would let a load with a budget that is already spent sail
            # through, and the test would prove nothing.
            raise requests.exceptions.ReadTimeout(
                f"the host did not answer within {timeout}s"
            )
        if url in self.served:
            return _FakeArchiveResponse(self.served[url])
        # The real host answers an unpublished file with 404.
        return _FakeArchiveResponse(b"", status_code=404)


# ==============================================================================
# D1: the archive frame is not the REST frame, and the difference is enforced
# ==============================================================================


@pytest.mark.asyncio
async def test_the_archive_frame_carries_the_record_of_how_it_differs(archive_server):
    """The difference is part of the FRAME, not a line in a log.

    The archive holds only closed bars, its trailing edge lags the REST
    path's by the publication lag and it sits on its own bar grid.  A caller
    has to be able to see that, from the API it is given, without reading
    a log file.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    # A REAL Binance month: 744 hourly bars.
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    difference = bulk_archive.frame_difference(frame)
    assert difference is not None, "the archive frame must say how it differs"

    # It says WHICH source produced it, and it is not the REST path.
    assert difference["source"] == "bulk_archive"
    assert difference["differs_from_rest"] is True

    # The closed-bars rule, which the REST path does not have.
    assert difference["closed_bars_only"] is True

    # The window that was asked for and the window that was delivered.
    assert difference["requested_start"] == "2024-01-01T00:00:00+00:00"
    assert difference["requested_end"] == "2024-01-31T23:59:59.999999+00:00"
    assert difference["actual_start"] == "2024-01-01T00:00:00+00:00"
    assert difference["actual_end"] == "2024-01-31T23:00:00+00:00"
    assert difference["rows"] == 744

    # The bar grid, MEASURED from the bars that were delivered.
    assert difference["bar_grid_phase_hours"] == 0.0
    assert difference["day_boundary_shift"] is None

    # This window is fully covered, and the record says so.
    assert difference["trailing_edge_short_by_seconds"] == 0.0


@pytest.mark.asyncio
async def test_the_trailing_edge_shortfall_is_measured_not_asserted(archive_server):
    """A window that runs past the archive's last bar says by how much.

    The archive lags the REST path by the publication lag, so the trailing
    edge of a window that runs past what Binance has published is short.
    The number is MEASURED from the last bar that was delivered, not
    copied from a constant.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Two days past the end of the last bar the archive holds: the last
    # bar of January opens on the 31st at 23:00 and closes at 00:00 on
    # the 1st of February.
    end = datetime(2024, 2, 2, 23, 59, 59, 999999, tzinfo=timezone.utc)

    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    difference = bulk_archive.frame_difference(frame)
    assert difference["actual_end"] == "2024-01-31T23:00:00+00:00"
    assert difference["trailing_edge_short_by_seconds"] == pytest.approx(
        2 * 24 * 3600 - 0.001, abs=0.01
    )
    assert difference["publication_lag_days"] == 2.0


@pytest.mark.asyncio
async def test_a_shifted_bar_grid_is_reported_as_a_day_boundary_shift(archive_server):
    """The grid offset is MEASURED, so a shifted archive is reported.

    The archives are Binance's own, and their bars sit on Binance's grid.
    If that grid is not the grid the REST path uses, the two frames do not
    line up bar for bar -- and the caller has to be told, because the
    difference changes every bar's timestamp.

    The shift can only be SEEN on a day-bucket interval: an hourly series
    shifted by a whole number of hours is the same series.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    # A month of DAILY bars that open eight hours off the UTC grid, the way
    # an archive aligned to 00:00 UTC+08:00 would.
    shifted = _make_rows(start + timedelta(hours=8), 31, 1440)
    archive_server.add(
        _monthly_url("BTCUSDT", "1d", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(shifted), "BTCUSDT-1d-2024-01.csv"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1d", start, end, FUTURES
    )

    difference = bulk_archive.frame_difference(frame)
    assert difference["bar_grid_phase_hours"] == 8.0
    shift = difference["day_boundary_shift"]
    assert shift is not None
    assert shift["shift_hours"] == 8.0
    assert "do not line up bar for bar" in shift["description"]


@pytest.mark.asyncio
async def test_the_loader_keeps_the_difference_record_on_the_frame_it_returns(
    archive_server, monkeypatch, tmp_path
):
    """The record has to survive the loader's OWN operations.

    ``pandas.concat`` DROPS the ``attrs`` when the frames it merges do not
    all carry the same ones, and the local cache (read from parquet)
    carries none.  The loader puts the records back on the frame it hands
    to the caller.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    cache_dir = tmp_path / "binance" / "futures" / "BTCUSDT"
    cache_dir.mkdir(parents=True)
    empty_cache = pd.DataFrame(
        {
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
        },
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
    )
    empty_cache.to_parquet(cache_dir / "kline_1h.parquet")

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is not None
    difference = bulk_archive.frame_difference(result)
    assert difference is not None, "the difference must survive the merge"
    assert difference["source"] == "bulk_archive"
    assert difference["closed_bars_only"] is True

    provenance = data_loader.klines_provenance(result)
    assert provenance is not None
    assert provenance["source"] == "bulk_archive"
    assert provenance["differs_from_rest"] is True
    assert provenance["requested_start"] == "2024-01-01T00:00:00+00:00"
    assert provenance["actual_start"] == "2024-01-01T00:00:00+00:00"
    assert provenance["rows"] == 744
    assert provenance["difference"] == difference


@pytest.mark.asyncio
async def test_an_archive_frame_whose_record_does_not_describe_it_is_refused(
    archive_server, monkeypatch
):
    """The difference is ENFORCED, not trusted.

    The record is the only thing that tells a caller that the frame it
    holds is not the frame the REST path would have returned.  A frame
    whose record does not describe it -- or that arrives without one -- is
    refused: a frame nobody can tell apart from a REST frame is exactly
    the failure this fallback must not have.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    rows = _make_rows(start, 24, 60)
    frame = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(
            [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows], name="open_time"
        ),
    )
    frame.attrs[data_loader.SOURCE_ATTR] = "bulk_archive"
    # A record that lies about the frame it travels with.
    frame.attrs[bulk_archive.DIFFERENCE_ATTR] = {
        "source": "bulk_archive",
        "actual_start": "1999-01-01T00:00:00+00:00",
        "actual_end": "1999-01-02T00:00:00+00:00",
    }
    monkeypatch.setattr(
        data_loader, "fetch_klines_from_bulk_archive", lambda *a, **k: frame
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is None, "a frame that cannot describe itself must not be served"


@pytest.mark.asyncio
async def test_an_archive_frame_without_any_record_is_refused(archive_server, monkeypatch):
    """A missing record is as bad as a wrong one."""
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    rows = _make_rows(start, 24, 60)
    frame = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(
            [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows], name="open_time"
        ),
    )
    frame.attrs[data_loader.SOURCE_ATTR] = "bulk_archive"
    monkeypatch.setattr(
        data_loader, "fetch_klines_from_bulk_archive", lambda *a, **k: frame
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is None, "an archive frame without a record must not be served"


# ==============================================================================
# D2: the leading-edge shortfall is ENFORCED
# ==============================================================================


def _serve_leading_window(archive_server, first_day: datetime, last_day: datetime):
    """Serves the daily files of the window's LAST days only.

    The leading days are left out on purpose: they are the ones the
    archive cannot prove anything about.
    """
    day = first_day
    while day <= last_day:
        if day == last_day:
            rows = _make_rows(day, 24, 60)
            archive_server.add(
                _daily_url("BTCUSDT", "1h", FUTURES, day.year, day.month, day.day),
                _make_zip(
                    _rows_to_csv(rows), f"BTCUSDT-1h-{day.year:04d}-{day.month:02d}-{day.day:02d}.csv"
                ),
            )
        day += timedelta(days=1)


@pytest.mark.asyncio
async def test_an_unproven_leading_shortfall_fails_the_load(
    archive_server, monkeypatch, frozen_clock, tmp_path
):
    """A frame that starts later than the window is REFUSED.

    The leading days the archive holds nothing for are still inside the
    publication lag, so their absence proves NOTHING: the symbol may have
    traded then and the file may simply not be published yet.  Serving the
    frame would hand the caller a window it did not ask for, so the load
    fails instead.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    now = frozen_clock(datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc))
    start = now - timedelta(days=2)
    end = now

    # Only the LAST day of the window has an archive.  The two days before
    # it are inside the publication lag, so the archive cannot prove that
    # the symbol did not trade then.
    _serve_leading_window(archive_server, start, end)

    with pytest.raises(bulk_archive.BulkArchiveCoverageError):
        bulk_archive.fetch_klines_from_bulk_archive(
            "BTCUSDT", "1h", start, end, FUTURES
        )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )
    assert result is None, "a frame shorter than the window must not be served"


@pytest.mark.asyncio
async def test_a_proven_leading_shortfall_is_served_and_named(
    archive_server, monkeypatch, frozen_clock
):
    """A shortfall the archive can PROVE is served -- and named.

    The leading days are older than the publication lag, so Binance would
    have published them if the symbol had traded.  Their absence PROVES it
    did not trade then, which means the REST path would have returned the
    same shorter frame.  The load succeeds and the shortfall is recorded.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    now = frozen_clock(datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc))
    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 23, tzinfo=timezone.utc)

    # Only the 20th has an archive: the 15th..19th are older than the
    # publication lag, so their absence is evidence.
    rows = _make_rows(datetime(2024, 1, 20, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 20),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01-20.csv"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert frame is not None
    assert frame.index[0] == pd.Timestamp("2024-01-20T00:00:00Z")

    gap = bulk_archive.coverage_gap(frame)
    assert gap is not None, "the shortfall must be recorded on the frame"
    assert gap["proven"] is True
    assert gap["gap_days"] == 5
    assert gap["requested_start"] == "2024-01-15T00:00:00+00:00"
    assert gap["first_bar"] == "2024-01-20T00:00:00+00:00"
    assert "2024-01-15" in gap["missing_leading_days"]

    difference = bulk_archive.frame_difference(frame)
    assert difference["leading_shortfall"]["proven"] is True


@pytest.mark.asyncio
async def test_a_window_with_no_shortfall_still_works(archive_server):
    """The normal case is untouched: the frame covers the window."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert frame is not None
    assert len(frame) == 744
    assert bulk_archive.coverage_gap(frame) is None
    difference = bulk_archive.frame_difference(frame)
    assert difference["leading_shortfall"] is None


# ==============================================================================
# D3: a usable local cache completes the load
# ==============================================================================


@pytest.mark.asyncio
async def test_a_usable_local_cache_completes_the_load_when_the_remote_fails(
    archive_server, monkeypatch, tmp_path
):
    """Candles that used to load must load.

    The remote source failed and there is nothing in the archive, but the
    local parquet cache holds the history that exists for the window.  The
    load COMPLETES with it -- and the frame says the remote failed, so the
    caller is not left believing it got the whole window.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    cache_dir = tmp_path / "binance" / "futures" / "BTCUSDT"
    cache_dir.mkdir(parents=True)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 23, tzinfo=timezone.utc)
    # The cache covers the FIRST HALF of the window only, so the loader
    # really does ask the remote source for the rest -- and the remote
    # source really does fail.
    index = pd.date_range(start, start + timedelta(hours=11), freq="1h", tz="UTC")
    cached = pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [10.0] * len(index),
        },
        index=index,
    )
    cached.index.name = "open_time"
    cached.to_parquet(cache_dir / "kline_1h.parquet")

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is not None, "a usable local cache must not be thrown away"
    assert len(result) == 12

    provenance = data_loader.klines_provenance(result)
    assert provenance is not None
    assert provenance["source"] == "local_cache"
    assert provenance["remote_failed"] is True
    assert "does not cover the whole window" in provenance["note"]


# ==============================================================================
# D4: the deadline is scaled, and a missing file is served and named
# ==============================================================================


@pytest.mark.asyncio
async def test_the_archive_budget_scales_with_the_window(monkeypatch):
    """A window of many months must not be killed by a fixed budget.

    Two years is 24 monthly archives plus the daily files of the trailing
    month.  On a link that answers every file, the load has to finish: the
    budget of a load is scaled to the window it serves.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)
    # A base budget that is far too small for the window: with a FIXED
    # budget the load runs out of time after a handful of files.
    monkeypatch.setattr(config, "BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS", 0.15)

    # The budget of a load is SCALED to its window: it covers one slice per
    # request the window can need, so a bigger window gets a bigger budget.
    expected_requests = bulk_archive._expected_request_count(24)
    assert bulk_archive._archive_budget_seconds(24) >= (
        expected_requests * config.BULK_ARCHIVE_PER_REQUEST_BUDGET_SECONDS
    )
    assert bulk_archive._archive_budget_seconds(24) > (
        bulk_archive._archive_budget_seconds(1)
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    served = {}
    month = start
    while month <= end:
        days = (month + timedelta(days=32)).replace(day=1) - month
        served[_monthly_url("BTCUSDT", "1d", FUTURES, month.year, month.month)] = (
            _make_zip(
                _rows_to_csv(_make_rows(month, days.days, 1440)),
                f"BTCUSDT-1d-{month.year:04d}-{month.month:02d}.csv",
            )
        )
        month = (month + timedelta(days=32)).replace(day=1)

    # Every file answers, but the host is slow: 0.02s per file.
    session = _SlowArchiveSession(served, delay=0.02)
    monkeypatch.setattr(bulk_archive, "http_session", session)

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1d", start, end, FUTURES
    )

    assert frame is not None, "a window must not be killed by its own size"
    assert len(frame) == 731
    assert len(session.requests) == 24


@pytest.mark.asyncio
async def test_a_genuinely_missing_daily_file_is_served_and_named(archive_server, monkeypatch):
    """A missing file inside the traded range is served -- and NAMED.

    The data that exists is worth more than no data, so the load does not
    die.  But the gap is recorded on the frame, so the caller knows
    exactly which buckets are missing instead of trading a hole blind.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    # The candles that exist are served: four whole days plus the first
    # thirteen hours of the 20th.  The 19th is the gap.
    assert frame is not None
    assert len(frame) == 4 * 24 + 13

    difference = bulk_archive.frame_difference(frame)
    missing = difference["missing_buckets"]
    assert len(missing) == 1, missing
    assert missing[0]["start"].startswith("2024-01-19")


@pytest.mark.asyncio
async def test_a_missing_file_is_logged_loudly(archive_server, monkeypatch, caplog):
    """The gap is not only on the frame: it is in the log too."""
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    with caplog.at_level(logging.ERROR, logger="bot_module.bulk_archive"):
        bulk_archive.fetch_klines_from_bulk_archive(
            "BTCUSDT", "1h", start, end, FUTURES
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.name == "bot_module.bulk_archive"
    ]
    assert any("MISSING 1 bucket" in message for message in messages), messages
    assert any("2024-01-19" in message for message in messages), messages


# ==============================================================================
# The source guards: a 451 that is not Binance's, and the COIN-M venue
# ==============================================================================


@pytest.mark.asyncio
async def test_a_451_that_is_not_binance_does_not_trigger_the_archive_fallback(
    archive_server, monkeypatch, tmp_path
):
    """HTTP 451 is also emitted by proxies and WAFs (RFC 7725).

    A proxy's 451 says nothing about Binance.  Answering it with archive
    data would swap the source of the candles without anybody knowing, so
    the fallback only runs for a 451 that BINANCE answered.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(data_loader, "_make_api_request", _proxy_blocked_rest_api)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is None
    assert archive_server.requested == [], "the archive must not answer a proxy's block"


# A body that says the request came from a restricted location and says
# NOTHING else.  "restricted location" is the GENERIC half of Binance's own
# answer -- it says the request was blocked because of where it came from,
# and a corporate proxy or a WAF can write exactly that sentence -- so a
# body that carries it ALONE does not identify Binance.
GENERIC_LOCATION_BLOCK_BODY = (
    "<html><head><title>451 Unavailable For Legal Reasons</title></head>"
    "<body>Access to this host from a restricted location has been denied. "
    "Contact helpdesk@example.com for an exception.</body></html>"
)


def _generic_location_blocked_rest_api(*args, **kwargs):
    """A 451 that says 'restricted location' and does NOT name Binance."""
    raise _http_error_with_body(
        451,
        GENERIC_LOCATION_BLOCK_BODY,
        "https://api.binance.com/api/v3/klines",
    )


@pytest.mark.asyncio
async def test_a_451_that_only_says_restricted_location_is_not_binance(
    archive_server, monkeypatch, tmp_path
):
    """The generic half of Binance's phrase is not enough to arm the fallback.

    Binance's own body is "Service unavailable from a restricted location
    according to 'b. Eligibility' in https://www.binance.com/en/terms", and
    BOTH halves of it identify Binance: the first one only says the request
    was blocked because of where it came from, which a proxy can say too.
    A body that carries the generic phrase and nothing else must not arm
    the archive, or the candles are swapped for archive data over a block
    that Binance never issued.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(data_loader, "_make_api_request", _generic_location_blocked_rest_api)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is None
    assert archive_server.requested == [], (
        "the archive must not answer a block that does not name Binance"
    )


@pytest.mark.asyncio
async def test_a_coinm_request_is_never_answered_with_usdtm_archive_data(
    archive_server, monkeypatch
):
    """A COIN-M request must not be answered with USDT-M candles.

    The venue is decided by the market type alone, in one place, and both
    the REST path and the archive ask it.  A COIN-M request goes to the
    COIN-M archive folder; the USDT-M folder is never touched.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", COINM, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, COINM
    )

    assert frame is not None
    assert len(frame) == 24
    assert archive_server.requested, "the archive must have been consulted"
    assert all("/futures/cm/" in url for url in archive_server.requested), (
        archive_server.requested
    )
    assert not any("/futures/um/" in url for url in archive_server.requested)


def test_the_rest_path_and_the_archive_agree_on_the_venue():
    """One venue per market type, decided in one place."""
    assert config.resolve_market_venue("spot") == "spot"
    assert config.resolve_market_venue("futures_usdtm") == "futures_usdtm"
    assert config.resolve_market_venue("futures_coinm") == "futures_coinm"
    assert config.resolve_market_venue("futures_cm") == "futures_coinm"
    assert config.resolve_market_venue("coinm") == "futures_coinm"
    # A market type that merely CONTAINS "spot" is not the spot venue.
    assert config.resolve_market_venue("spot_usdtm") == "futures_usdtm"
    assert config.resolve_market_venue("spot_perp") == "futures_usdtm"

    assert bulk_archive._archive_market_path("spot") == "spot"
    assert bulk_archive._archive_market_path("futures_usdtm") == "futures/um"
    assert bulk_archive._archive_market_path("futures_coinm") == "futures/cm"
    assert bulk_archive._archive_market_path("spot_usdtm") == "futures/um"
