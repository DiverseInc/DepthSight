# tests/test_data_loader_bulk_archive.py
"""Tests for the Binance bulk-archive klines fallback.

The Binance REST market-data endpoints (``api.binance.com`` /
``fapi.binance.com``) answer HTTP 451 from geo/IP-blocked networks, so every
backtest fails with "Failed to load historical data for <symbol>."  Binance
publishes the same candles on ``data.binance.vision``, and
``bot_module.bulk_archive`` reads them as a third source.

NO NETWORK IS USED HERE.  The archives are built locally as real ``.zip``
files containing a real CSV, and the HTTP fetch is stubbed.
"""

import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from bot_module import bulk_archive, config, data_loader

# Binance's own header for the 12 positional archive columns.
ARCHIVE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)

INTERVAL_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

FUTURES = "futures_usdtm"
SPOT = "spot"


# ==============================================================================
# Fixture builders
# ==============================================================================


def _make_rows(start_utc, count, interval_minutes, base_price=100.0):
    """Builds kline rows shaped exactly like the Binance REST API response."""
    rows = []
    open_time = int(start_utc.timestamp() * 1000)
    step = interval_minutes * 60 * 1000
    for i in range(count):
        o = base_price + i
        h = o + 2.0
        low = o - 2.0
        c = o + 1.0
        v = 10.0 + i
        rows.append(
            [
                open_time,
                f"{o:.8f}",
                f"{h:.8f}",
                f"{low:.8f}",
                f"{c:.8f}",
                f"{v:.8f}",
                open_time + step - 1,
                f"{v * c:.8f}",
                100 + i,
                f"{v / 2:.8f}",
                f"{v * c / 2:.8f}",
                "0",
            ]
        )
        open_time += step
    return rows


def _rows_to_csv(rows, header=True):
    lines = []
    if header:
        lines.append(ARCHIVE_HEADER)
    for row in rows:
        lines.append(",".join(str(value) for value in row))
    return "\n".join(lines) + "\n"


def _make_zip(csv_text, member_name="BTCUSDT-1h-2024-01.csv"):
    """Builds a real .zip holding one CSV, like Binance's archives."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, csv_text)
    payload = buffer.getvalue()
    # The live bulk-archive responses begin with the ZIP magic bytes.
    assert payload[:4] == b"PK\x03\x04", "fixture is not a real ZIP archive"
    return payload


class FakeArchiveServer:
    """Stands in for data.binance.vision. Serves locally built .zip files."""

    def __init__(self):
        self.files = {}
        self.requested = []

    def add(self, url, payload):
        assert payload[:4] == b"PK\x03\x04", (
            "the served payload must be a real ZIP, not an error page"
        )
        self.files[url] = payload
        return self

    def fetch(self, url):
        self.requested.append(url)
        return self.files.get(url)

    def monthly_urls(self):
        return [url for url in self.requested if "/monthly/" in url]

    def daily_urls(self):
        return [url for url in self.requested if "/daily/" in url]


@pytest.fixture
def archive_server(monkeypatch):
    server = FakeArchiveServer()
    monkeypatch.setattr(bulk_archive, "fetch_archive_bytes", server.fetch)
    return server


def _blocked_rest_api(*args, **kwargs):
    """Simulates the measured HTTP 451 from the Binance REST endpoints."""
    response = requests.Response()
    response.status_code = 451
    response.reason = "Unavailable For Legal Reasons"
    response.url = "https://api.binance.com/api/v3/klines"
    raise requests.exceptions.HTTPError(
        "451 Client Error: Unavailable For Legal Reasons For url: "
        "https://api.binance.com/api/v3/klines",
        response=response,
    )


def _working_rest_api(rows):
    """A REST API mock that honours startTime/endTime like the real one."""

    def _mock(endpoint, params=None, market_type="spot"):
        start = (params or {}).get("startTime")
        end = (params or {}).get("endTime")
        return [
            row
            for row in rows
            if (start is None or row[0] >= start) and (end is None or row[0] <= end)
        ]

    return _mock


def _monthly_url(symbol, interval, market_type, year, month):
    market = "spot" if "spot" in market_type else "futures/um"
    period = f"{year:04d}-{month:02d}"
    return (
        f"{config.BINANCE_BULK_ARCHIVE_DATA_URL}/{market}/monthly/klines/"
        f"{symbol}/{interval}/{symbol}-{interval}-{period}.zip"
    )


def _daily_url(symbol, interval, market_type, year, month, day):
    market = "spot" if "spot" in market_type else "futures/um"
    period = f"{year:04d}-{month:02d}-{day:02d}"
    return (
        f"{config.BINANCE_BULK_ARCHIVE_DATA_URL}/{market}/daily/klines/"
        f"{symbol}/{interval}/{symbol}-{interval}-{period}.zip"
    )


def _add_month(month_start):
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def _month_starts(start_dt, end_dt):
    months = []
    month = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= end_dt:
        months.append(month)
        month = _add_month(month)
    return months


class _FrozenDatetime(datetime):
    """A datetime whose ``now()`` is pinned, so "the past" is deterministic."""

    FROZEN = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.FROZEN.astimezone(tz) if tz else cls.FROZEN


@pytest.fixture
def frozen_now(monkeypatch):
    """Pins the archive module's clock so the publication-lag rule is testable."""
    monkeypatch.setattr(bulk_archive, "datetime", _FrozenDatetime)
    return _FrozenDatetime.FROZEN


# ==============================================================================
# (a) The fallback is used when the REST path raises 451
# ==============================================================================


@pytest.mark.asyncio
async def test_bulk_archive_is_used_when_rest_api_returns_451(
    archive_server, monkeypatch
):
    """REST 451 + no local cache => the bulk archive supplies the candles."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

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

    assert result is not None
    assert len(result) == 24
    assert archive_server.monthly_urls() == [
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1)
    ]


@pytest.mark.asyncio
async def test_rest_api_still_wins_when_it_works(archive_server, monkeypatch):
    """The fallback is additive: a working REST path makes no archive request."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    monkeypatch.setattr(
        data_loader, "_make_api_request", _working_rest_api(rows)
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is not None
    assert len(result) == 24
    assert archive_server.requested == []


# ==============================================================================
# (b) The archive output is indistinguishable from the REST path's output
# ==============================================================================


@pytest.mark.asyncio
async def test_archive_output_matches_rest_api_output(archive_server, monkeypatch):
    """Same candles => identical index, columns, dtypes and values."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)

    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    # What the REST path produces.
    monkeypatch.setattr(
        data_loader, "_make_api_request", _working_rest_api(rows)
    )
    rest_frame = data_loader._download_klines_from_api(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    # What the bulk-archive fallback produces.
    archive_frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert rest_frame is not None and not rest_frame.empty
    assert archive_frame is not None and not archive_frame.empty

    pd.testing.assert_frame_equal(rest_frame, archive_frame)

    # Explicitly assert the properties the loader depends on.
    assert archive_frame.index.tz is not None
    assert str(archive_frame.index.tz) == "UTC"
    assert archive_frame.index.name == "open_time"
    assert list(archive_frame.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "number_of_trades",
    ]
    assert archive_frame["number_of_trades"].dtype == rest_frame[
        "number_of_trades"
    ].dtype
    assert archive_frame["close"].dtype == rest_frame["close"].dtype


@pytest.mark.asyncio
async def test_archive_output_matches_rest_api_output_without_header(
    archive_server, monkeypatch
):
    """Some archives have NO header row; the parser must still match the REST path."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)

    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows, header=False), "BTCUSDT-1h-2024-01.csv"),
    )

    monkeypatch.setattr(
        data_loader, "_make_api_request", _working_rest_api(rows)
    )
    rest_frame = data_loader._download_klines_from_api(
        "BTCUSDT", "1h", start, end, FUTURES
    )
    archive_frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    pd.testing.assert_frame_equal(rest_frame, archive_frame)


# ==============================================================================
# (c) A window spanning two months
# ==============================================================================


@pytest.mark.asyncio
async def test_two_month_window_concatenates_in_time_order(archive_server):
    """Both months are pulled and concatenated in order, without duplicates."""
    jan_rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24, 60)
    feb_rows = _make_rows(datetime(2024, 2, 1, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(jan_rows), "BTCUSDT-1h-2024-01.csv"),
    )
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 2),
        _make_zip(_rows_to_csv(feb_rows), "BTCUSDT-1h-2024-02.csv"),
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert len(frame) == 48
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()
    assert frame.index[0] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-02-01T23:00:00Z")
    assert len(archive_server.monthly_urls()) == 2


@pytest.mark.asyncio
async def test_mid_month_window_uses_the_monthly_archive_and_is_clipped(
    archive_server
):
    """A window that ends mid-month costs ONE monthly request, clipped to it."""
    jan_rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24 * 31, 60)
    feb_rows = _make_rows(datetime(2024, 2, 1, tzinfo=timezone.utc), 24 * 29, 60)
    mar_rows = _make_rows(datetime(2024, 3, 1, tzinfo=timezone.utc), 24 * 31, 60)
    for year, month, rows in ((2024, 1, jan_rows), (2024, 2, feb_rows), (2024, 3, mar_rows)):
        archive_server.add(
            _monthly_url("BTCUSDT", "1h", FUTURES, year, month),
            _make_zip(
                _rows_to_csv(rows), f"BTCUSDT-1h-{year:04d}-{month:02d}.csv"
            ),
        )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 3, 15, 12, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    # Three monthly archives, and NOT ONE daily file.
    assert len(archive_server.monthly_urls()) == 3
    assert archive_server.daily_urls() == []
    # Clipped to the window: 31 + 29 whole days of hours, then 1 March to
    # the 15th at 12:00 inclusive.
    assert len(frame) == 24 * 31 + 24 * 29 + 14 * 24 + 13
    assert frame.index[0] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-03-15T12:00:00Z")
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()


@pytest.mark.asyncio
async def test_daily_archives_are_used_when_the_monthly_is_not_published(
    archive_server
):
    """The monthly archive not being published yet is what selects the dailies."""
    for day in range(15, 21):
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

    assert archive_server.monthly_urls() == [
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1)
    ]
    assert len(archive_server.daily_urls()) == 6  # 15th .. 20th
    assert len(frame) == 5 * 24 + 13
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()
    assert frame.index[0] == pd.Timestamp("2024-01-15T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-01-20T12:00:00Z")


@pytest.mark.asyncio
async def test_rows_outside_the_window_are_excluded(archive_server):
    """Only the candles inside the requested window come back."""
    rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    start = datetime(2024, 1, 1, 5, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 9, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert len(frame) == 5
    assert frame.index[0] == pd.Timestamp("2024-01-01T05:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-01-01T09:00:00Z")


# ==============================================================================
# (d) Coverage: a hole must fail loudly, a publication lag must not
# ==============================================================================


@pytest.mark.asyncio
async def test_missing_past_day_fails_loudly_instead_of_returning_a_gap(
    archive_server, monkeypatch
):
    """A day that should be published but is not is a HOLE: fail, do not ship it."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    # Every day of the window except the 19th.
    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    # The archive refuses to return a gapped frame.
    with pytest.raises(bulk_archive.BulkArchiveCoverageError):
        bulk_archive.fetch_klines_from_bulk_archive(
            "BTCUSDT", "1h", start, end, FUTURES
        )

    # And the loader reports "no data", not a frame with a hole in it.
    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_future_day_within_publication_lag_still_succeeds(
    archive_server, monkeypatch, frozen_now, caplog
):
    """The trailing edge legitimately has no file yet; that is not a hole."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    now = frozen_now
    start = now - timedelta(days=5)

    # Serve the monthly archive for every month the window touches EXCEPT the
    # current one, which Binance has not published yet.
    for month in _month_starts(start, now):
        if month.year == now.year and month.month == now.month:
            continue
        rows = _make_rows(month, 24, 60)
        archive_server.add(
            _monthly_url("BTCUSDT", "1h", FUTURES, month.year, month.month),
            _make_zip(
                _rows_to_csv(rows),
                f"BTCUSDT-1h-{month.year:04d}-{month.month:02d}.csv",
            ),
        )

    # Serve the daily files of the current month up to three days ago; the
    # last three days are within the publication lag and have no file yet.
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= now:
        if day + timedelta(days=3) <= now:
            rows = _make_rows(day, 24, 60)
            archive_server.add(
                _daily_url(
                    "BTCUSDT", "1h", FUTURES, day.year, day.month, day.day
                ),
                _make_zip(
                    _rows_to_csv(rows),
                    f"BTCUSDT-1h-{day.year:04d}-{day.month:02d}-{day.day:02d}.csv",
                ),
            )
        day += timedelta(days=1)

    with caplog.at_level(logging.INFO, logger="bot_module.bulk_archive"):
        frame = bulk_archive.fetch_klines_from_bulk_archive(
            "BTCUSDT", "1h", start, now, FUTURES
        )

    # The load succeeds: the trailing gap is a publication lag, not a hole.
    assert frame is not None
    assert not frame.empty
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()

    # And the gap is REPORTED, not hidden.
    messages = [record.getMessage() for record in caplog.records]
    assert any("days_missing=3" in message for message in messages), messages


# ==============================================================================
# (e) The None contract and the local cache
# ==============================================================================


@pytest.mark.asyncio
async def test_rest_451_and_archive_failure_returns_none(archive_server, monkeypatch):
    """When BOTH sources fail the loader returns None, not an empty frame."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    def _explode(url):
        raise RuntimeError("the archive host is down too")

    monkeypatch.setattr(bulk_archive, "fetch_archive_bytes", _explode)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    # None, NOT an empty DataFrame: callers key on the difference.
    assert result is None


@pytest.mark.asyncio
async def test_missing_archive_yields_empty_result_without_raising(
    archive_server, monkeypatch
):
    """Nothing published for a window that is not in the past => empty, no raise."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    # A window that has not happened yet cannot have a coverage hole.
    start = datetime.now(timezone.utc) + timedelta(days=30)
    end = start + timedelta(days=1)

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )
    assert frame is None or frame.empty

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )
    assert result is None or result.empty


@pytest.mark.asyncio
async def test_non_451_api_error_keeps_todays_behaviour(
    archive_server, monkeypatch
):
    """Only a location block switches source; other API errors behave as before."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    def _server_error(*args, **kwargs):
        raise requests.exceptions.HTTPError("500 Server Error")

    monkeypatch.setattr(data_loader, "_make_api_request", _server_error)

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    # Same as before the fallback existed: the error propagates and the
    # loader yields nothing. The archive is NOT consulted for a 500.
    assert result is None
    assert archive_server.requested == []


@pytest.mark.asyncio
async def test_testnet_never_uses_the_mainnet_archive(archive_server, monkeypatch):
    """data.binance.vision holds MAINNET candles: testnet must never use it."""
    monkeypatch.setattr(config, "ACTIVE_TRADING_ENVIRONMENT", "testnet")
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

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

    # The archive was never consulted, so no mainnet candle can appear.
    assert archive_server.requested == []
    assert result is None


@pytest.mark.asyncio
async def test_local_cache_is_used_first_and_makes_no_archive_request(
    archive_server, monkeypatch, tmp_path
):
    """A complete local parquet short-circuits before any HTTP happens."""
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))

    cache_dir = tmp_path / "binance" / "futures" / "BTCUSDT"
    cache_dir.mkdir(parents=True)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 23, tzinfo=timezone.utc)
    index = pd.date_range(start, end, freq="1h", tz="UTC")
    cached = pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [10.0] * len(index),
            "natr": [1.0] * len(index),
            "is_volume_spike": [False] * len(index),
        },
        index=index,
    )
    cached.index.name = "open_time"
    cached.to_parquet(cache_dir / "kline_1h.parquet")

    def _explode(*args, **kwargs):
        raise AssertionError("no network call was expected")

    monkeypatch.setattr(data_loader, "_make_api_request", _explode)

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is not None
    assert len(result) == 24
    assert archive_server.requested == []


# ==============================================================================
# The four timeframes the trainer requires
# ==============================================================================


@pytest.mark.parametrize(
    "interval,candles_in_month",
    [
        ("1m", 31 * 24 * 60),
        ("1h", 31 * 24),
        ("4h", 31 * 6),
        ("1d", 31),
    ],
)
@pytest.mark.asyncio
async def test_every_required_timeframe_loads_from_the_archive(
    archive_server, monkeypatch, interval, candles_in_month
):
    """kline_1m, kline_1h, kline_4h and kline_1d all resolve via the archive."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, candles_in_month, INTERVAL_MINUTES[interval])
    archive_server.add(
        _monthly_url("BTCUSDT", interval, FUTURES, 2024, 1),
        _make_zip(
            _rows_to_csv(rows), f"BTCUSDT-{interval}-2024-01.csv"
        ),
    )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe=interval,
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is not None
    assert len(result) == candles_in_month
    assert result.index.is_monotonic_increasing
    assert not result.index.duplicated().any()
    assert str(result.index.tz) == "UTC"


@pytest.mark.parametrize(
    "year,month,days_in_month",
    [
        (2024, 1, 31),
        (2024, 2, 29),  # leap year
        (2024, 4, 30),
        (2025, 2, 28),
    ],
)
@pytest.mark.asyncio
async def test_complete_month_yields_hours_in_month_rows(
    archive_server, year, month, days_in_month
):
    """A complete month yields exactly hours-in-month candles, not a constant."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year, month, days_in_month, 23, 59, 59, 999999, tzinfo=timezone.utc)
    )
    expected = days_in_month * 24
    rows = _make_rows(start, expected, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, year, month),
        _make_zip(
            _rows_to_csv(rows), f"BTCUSDT-1h-{year:04d}-{month:02d}.csv"
        ),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert len(frame) == expected
    assert frame.index[0] == pd.Timestamp(f"{year:04d}-{month:02d}-01T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp(
        f"{year:04d}-{month:02d}-{days_in_month:02d}T23:00:00Z"
    )


# ==============================================================================
# The source of a load is recorded in the log
# ==============================================================================


@pytest.mark.asyncio
async def test_load_source_is_recorded_in_the_log(
    archive_server, monkeypatch, caplog
):
    """The log records WHICH source produced the candles."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    with caplog.at_level(logging.INFO, logger="bot_module.data_loader"):
        await data_loader.download_klines(
            symbol="BTCUSDT",
            timeframe="1h",
            start_dt=start,
            end_dt=end,
            market_type=FUTURES,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("klines_source=bulk_archive" in message for message in messages)


@pytest.mark.asyncio
async def test_rest_source_is_recorded_in_the_log(archive_server, monkeypatch, caplog):
    """When the REST API answers, the log says so."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    monkeypatch.setattr(
        data_loader, "_make_api_request", _working_rest_api(rows)
    )

    with caplog.at_level(logging.INFO, logger="bot_module.data_loader"):
        await data_loader.download_klines(
            symbol="BTCUSDT",
            timeframe="1h",
            start_dt=start,
            end_dt=end,
            market_type=FUTURES,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("klines_source=rest_api" in message for message in messages)
    assert not any("klines_source=bulk_archive" in message for message in messages)


# ==============================================================================
# Measured archive quirks
# ==============================================================================


@pytest.mark.asyncio
async def test_spot_archive_without_header_and_microsecond_timestamps(
    archive_server, monkeypatch
):
    """Spot archives have no header and use microseconds since 2025-01-01."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    micro_rows = [[row[0] * 1000] + row[1:] for row in rows]

    archive_server.add(
        _monthly_url("BTCUSDT", "1h", SPOT, 2025, 1),
        _make_zip(_rows_to_csv(micro_rows, header=False), "BTCUSDT-1h-2025-01.csv"),
    )

    monkeypatch.setattr(
        data_loader, "_make_api_request", _working_rest_api(rows)
    )
    rest_frame = data_loader._download_klines_from_api(
        "BTCUSDT", "1h", start, end, SPOT
    )
    archive_frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, SPOT
    )

    pd.testing.assert_frame_equal(rest_frame, archive_frame)
    assert archive_frame.index[0] == pd.Timestamp("2025-01-01T00:00:00Z")
