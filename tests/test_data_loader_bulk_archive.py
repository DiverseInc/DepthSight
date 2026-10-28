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
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import requests

from bot_module import bulk_archive, config, data_loader

# REAL Binance archives, committed as fixtures so the parser is pinned to
# real values instead of synthetic rows.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "binance_bulk_archive"

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
        assert (
            payload[:4] == b"PK\x03\x04"
        ), "the served payload must be a real ZIP, not an error page"
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


def _fixture(name):
    """Reads one REAL committed Binance archive."""
    path = FIXTURE_DIR / name
    assert path.exists(), f"missing fixture: {path}"
    payload = path.read_bytes()
    assert payload[:4] == b"PK\x03\x04", f"{name} is not a real ZIP archive"
    return payload


def _month_starts(start_dt, end_dt):
    months = []
    month = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= end_dt:
        months.append(month)
        month = _add_month(month)
    return months


def _epoch_day(moment):
    """The epoch day number of ``moment`` (UTC)."""
    return int(moment.timestamp()) // 86400


def _truncate_monthly_archive(payload, drop_days):
    """A REAL monthly archive with whole days removed.

    The committed fixture is a real Binance month (744 hourly bars, all 31
    days of January 2024).  Dropping the hours of one interior day from it
    produces exactly the defect the monthly path has to catch: a PUBLISHED
    archive that is missing days of its own month.  Returns the new payload
    and the number of bars that were removed.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        text = archive.read(name).decode("utf-8-sig")

    lines = text.splitlines()
    header = None
    if lines and lines[0].startswith("open_time"):
        header = lines[0]
        lines = lines[1:]

    kept = []
    dropped = 0
    for line in lines:
        stamp = int(line.split(",")[0])
        if stamp > 10**14:  # some archives are in microseconds
            stamp //= 1000
        if stamp // 86_400_000 in drop_days:  # the epoch day of the bar
            dropped += 1
            continue
        kept.append(line)

    assert dropped, "the fixture did not contain the day(s) that were dropped"
    body = ([header] if header else []) + kept
    return _make_zip("\n".join(body) + "\n", Path(name).name), dropped


class _FakeArchiveResponse:
    """A 200 answer carrying a real archive payload."""

    def __init__(self, content):
        self.status_code = 200
        self.content = content

    def raise_for_status(self):
        return None


class _NeverAnswersSession:
    """A transport that never answers, the way an unreachable host behaves.

    ``served`` holds the URLs that answer immediately with a real archive;
    every other URL runs into the request's timeout, exactly like a host
    that has gone away.
    """

    def __init__(self, served=None):
        self.served = served or {}
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(url)
        if url in self.served:
            return _FakeArchiveResponse(self.served[url])
        time.sleep(timeout)
        raise requests.exceptions.ReadTimeout("the archive host never answered")


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
    monkeypatch.setattr(data_loader, "_make_api_request", _working_rest_api(rows))

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
    monkeypatch.setattr(data_loader, "_make_api_request", _working_rest_api(rows))
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
    assert (
        archive_frame["number_of_trades"].dtype == rest_frame["number_of_trades"].dtype
    )
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

    monkeypatch.setattr(data_loader, "_make_api_request", _working_rest_api(rows))
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
    # COMPLETE months: a monthly archive that is missing days of its own
    # month is a hole and is refused (see
    # test_a_truncated_monthly_archive_is_refused_instead_of_returning_a_gap).
    jan_rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24 * 31, 60)
    feb_rows = _make_rows(datetime(2024, 2, 1, tzinfo=timezone.utc), 24 * 29, 60)
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

    assert len(frame) == 24 * 31 + 24 * 29
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()
    assert frame.index[0] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-02-29T23:00:00Z")
    assert len(archive_server.monthly_urls()) == 2


@pytest.mark.asyncio
async def test_mid_month_window_uses_the_monthly_archive_and_is_clipped(archive_server):
    """A window that ends mid-month costs ONE monthly request, clipped to it."""
    jan_rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24 * 31, 60)
    feb_rows = _make_rows(datetime(2024, 2, 1, tzinfo=timezone.utc), 24 * 29, 60)
    mar_rows = _make_rows(datetime(2024, 3, 1, tzinfo=timezone.utc), 24 * 31, 60)
    for year, month, rows in (
        (2024, 1, jan_rows),
        (2024, 2, feb_rows),
        (2024, 3, mar_rows),
    ):
        archive_server.add(
            _monthly_url("BTCUSDT", "1h", FUTURES, year, month),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-{year:04d}-{month:02d}.csv"),
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
    archive_server,
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
                _daily_url("BTCUSDT", "1h", FUTURES, day.year, day.month, day.day),
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
    assert any("days_absent=3" in message for message in messages), messages


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
async def test_non_451_api_error_keeps_todays_behaviour(archive_server, monkeypatch):
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
        _make_zip(_rows_to_csv(rows), f"BTCUSDT-{interval}-2024-01.csv"),
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
    end = datetime(year, month, days_in_month, 23, 59, 59, 999999, tzinfo=timezone.utc)
    expected = days_in_month * 24
    rows = _make_rows(start, expected, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, year, month),
        _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-{year:04d}-{month:02d}.csv"),
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
async def test_load_source_is_recorded_in_the_log(archive_server, monkeypatch, caplog):
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
    monkeypatch.setattr(data_loader, "_make_api_request", _working_rest_api(rows))

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

    monkeypatch.setattr(data_loader, "_make_api_request", _working_rest_api(rows))
    rest_frame = data_loader._download_klines_from_api(
        "BTCUSDT", "1h", start, end, SPOT
    )
    archive_frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, SPOT
    )

    pd.testing.assert_frame_equal(rest_frame, archive_frame)
    assert archive_frame.index[0] == pd.Timestamp("2025-01-01T00:00:00Z")


# ==============================================================================
# R1: "not listed yet" is not a hole; a mid-range hole still is
# ==============================================================================


@pytest.mark.asyncio
async def test_window_starting_before_the_listing_loads_the_shorter_history(
    archive_server, monkeypatch
):
    """The symbol was not listed until June: the archive holds no Jan-May data.

    That is SHORT HISTORY, not a hole: the load must succeed with the bars
    the archive actually has, instead of dying because the pre-listing months
    have no published archive.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    june_rows = _make_rows(datetime(2024, 6, 1, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 6),
        _make_zip(_rows_to_csv(june_rows), "BTCUSDT-1h-2024-06.csv"),
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 6, 30, 23, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    # No exception, and only the history the archive actually holds.
    assert frame is not None
    assert not frame.empty
    assert frame.index[0] == pd.Timestamp("2024-06-01T00:00:00Z")
    assert len(frame) == 24


@pytest.mark.asyncio
async def test_a_mid_range_hole_still_fails_loudly(archive_server, monkeypatch):
    """The symbol traded on BOTH sides of 19 January: its archive is missing.

    That IS a hole, and the load must fail rather than ship a gapped frame.
    This is the guard on the R1 relaxation: it must not let real holes
    through.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    # January has no monthly archive; its daily files are used instead, and
    # the 19th is missing.
    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )
    # February IS published, so the symbol demonstrably traded after the hole.
    feb_rows = _make_rows(datetime(2024, 2, 1, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 2),
        _make_zip(_rows_to_csv(feb_rows), "BTCUSDT-1h-2024-02.csv"),
    )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 2, 29, 23, tzinfo=timezone.utc)

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
    assert result is None


@pytest.mark.asyncio
async def test_an_empty_daily_file_is_not_a_hole(archive_server, monkeypatch):
    """A file that EXISTS with no candles means "no trades that day"."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    for day in (15, 17):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )
    # The 16th exists but holds no candles: the symbol did not trade.
    archive_server.add(
        _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 16),
        _make_zip(_rows_to_csv([]), "BTCUSDT-1h-2024-01-16.csv"),
    )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 17, 23, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert frame is not None
    assert len(frame) == 48


# ==============================================================================
# R2: the wrapper returns None whenever it cannot produce candles
# ==============================================================================


@pytest.mark.asyncio
async def test_archive_with_no_candles_yields_none_not_an_empty_frame(
    archive_server, monkeypatch
):
    """REST 451 + an archive with nothing => None, so the hard stops fire.

    The window is in the FUTURE: there is no coverage hole to raise, so the
    archive simply has no candles.  That must still surface as None, not as
    an empty frame -- an empty frame is "loaded, but empty", and the callers
    treat the two differently.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    start = datetime.now(timezone.utc) + timedelta(days=30)
    end = start + timedelta(days=1)

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is None, "an empty frame is not 'no data loaded'"


@pytest.mark.asyncio
async def test_archive_coverage_failure_also_yields_none(archive_server, monkeypatch):
    """REST 451 + an archive that cannot cover the window => None."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)
    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is None


# ==============================================================================
# R3: the archive picks its venue exactly like the REST path does
# ==============================================================================


def test_archive_market_path_uses_the_rest_path_comparison():
    """Exactly the comparison the REST path uses (market_type == "spot")."""
    assert bulk_archive._archive_market_path("spot") == "spot"
    assert bulk_archive._archive_market_path("futures_usdtm") == "futures/um"
    # The REST path sends EVERY non-spot market type to the USDT-M host, so
    # the archive must answer a COIN-M request with USDT-M candles.  Serving
    # COIN-M candles here would swap the venue under the caller.
    assert bulk_archive._archive_market_path("futures_coinm") == "futures/um"
    assert bulk_archive._archive_market_path("futures_cm") == "futures/um"
    assert bulk_archive._archive_market_path("coinm") == "futures/um"
    # A market type that merely CONTAINS "spot" must NOT get spot candles:
    # the REST path would have queried the futures host.
    assert bulk_archive._archive_market_path("spot_usdtm") == "futures/um"
    assert bulk_archive._archive_market_path("spot_perp") == "futures/um"


# The suite's autouse fixture replaces ``data_loader._make_api_request`` with a
# stub so that no test can make a real network call (tests/conftest.py:738).
# This test needs the REAL one, so the original is captured here at import
# time: module import happens during collection, before any fixture runs.
_REAL_MAKE_API_REQUEST = data_loader._make_api_request


def test_archive_venue_matches_the_rest_host_for_every_market_type(monkeypatch):
    """The archive must be a FALLBACK for the REST path, never a second venue.

    ``data_loader._make_api_request`` sends ``market_type == "spot"`` to the
    spot host and EVERY other market type -- including the COIN-M ones -- to
    the USDT-M futures host.  If the archive folder disagrees for even one
    market type, a fallback would answer that request with a DIFFERENT
    market's candles and change the numbers the backtest reports.

    This test reads the REST path's OWN choice, so the two cannot drift apart
    again the way a hand-written pair of lists would.
    """
    captured = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            captured.append(url)
            return _FakeResponse()

    monkeypatch.setattr(data_loader, "http_session", _FakeSession())

    if config.ACTIVE_TRADING_ENVIRONMENT == "testnet":
        spot_host = config.BINANCE_SPOT_TESTNET_API_URL_FOR_LOADER
    else:
        spot_host = config.BINANCE_SPOT_DATA_API_URL_FOR_LOADER

    for market_type in (
        "spot",
        "futures_usdtm",
        "futures_coinm",
        "futures_cm",
        "coinm",
        "spot_usdtm",
        "spot_perp",
        "",
    ):
        captured.clear()
        _REAL_MAKE_API_REQUEST("klines", {}, market_type=market_type)
        assert captured, (
            "the real _make_api_request did not issue a request; the suite's "
            "stub was captured instead of the original"
        )
        rest_host = captured[0]

        rest_uses_spot = rest_host.startswith(spot_host)
        archive_folder = bulk_archive._archive_market_path(market_type)

        if rest_uses_spot:
            assert archive_folder == "spot", market_type
        else:
            assert archive_folder == "futures/um", market_type


@pytest.mark.asyncio
async def test_a_transport_failure_is_not_treated_as_a_location_block(
    archive_server, monkeypatch
):
    """A timeout/DNS failure must NOT silently become archive data.

    Only HTTP 451 is a location block.  A transport failure is a genuine
    fault, and the archive's trailing edge is short by the publication lag --
    so serving it in place of the fault could quietly shorten a backtest
    window.  The fault must be re-raised instead.
    """

    def _timeout(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(data_loader, "_make_api_request", _timeout)

    rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        data_loader._download_klines_from_api_with_archive_fallback(
            "BTCUSDT",
            "1h",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
            FUTURES,
        )


@pytest.mark.asyncio
async def test_a_market_type_containing_spot_does_not_get_spot_candles(
    archive_server, monkeypatch
):
    """ "spot_usdtm" must resolve to the FUTURES archive, like the REST path."""
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 24, 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-1h-2024-01.csv"),
    )

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT",
        "1h",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
        "spot_usdtm",
    )

    assert len(frame) == 24
    # The futures archive was requested; the spot one was not.
    assert archive_server.monthly_urls() == [
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1)
    ]
    assert not any("/spot/" in url for url in archive_server.requested)


# ==============================================================================
# R4: the parser is pinned to REAL Binance values
# ==============================================================================


@pytest.mark.asyncio
async def test_real_futures_archive_values_are_pinned(archive_server):
    """Real Binance bytes: exact row count and exact first/last bar."""
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
    )
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 2),
        _fixture("BTCUSDT-1h-2024-02-futures-um.zip"),
    )
    archive_server.add(
        _daily_url("BTCUSDT", "1h", FUTURES, 2024, 3, 1),
        _fixture("BTCUSDT-1h-2024-03-01-futures-um.zip"),
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 3, 1, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    # 744 (Jan) + 696 (Feb) + 1 (the 1 March bar) = 1441
    assert len(frame) == 1441
    assert frame.index[0] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-03-01T00:00:00Z")
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()

    first = frame.iloc[0]
    assert first["open"] == 42314.0
    assert first["high"] == 42603.2
    assert first["low"] == 42289.6
    assert first["close"] == 42503.5
    assert first["volume"] == 8459.477
    assert first["number_of_trades"] == 88278

    # The single month on its own is exactly the 744 hours of January.
    january = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT",
        "1h",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
        FUTURES,
    )
    assert len(january) == 744
    assert january.index[0] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert january.index[-1] == pd.Timestamp("2024-01-31T23:00:00Z")


@pytest.mark.asyncio
async def test_real_spot_archive_values_differ_from_futures(archive_server):
    """The spot archive is a DIFFERENT venue: pin both and assert they differ."""
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", SPOT, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-spot.zip"),
    )
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    spot = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, SPOT
    )

    assert len(spot) == 744
    first = spot.iloc[0]
    assert first["open"] == 42283.58
    assert first["number_of_trades"] == 47134

    # The same window on the futures venue is a different market entirely.
    futures = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )
    assert len(futures) == 744
    assert futures.iloc[0]["open"] == 42314.0
    assert futures.iloc[0]["number_of_trades"] == 88278
    assert spot.iloc[0]["open"] != futures.iloc[0]["open"]
    assert spot.iloc[0]["number_of_trades"] != futures.iloc[0]["number_of_trades"]


# ==============================================================================
# R5: a MONTHLY archive can be incomplete too
# ==============================================================================


@pytest.mark.asyncio
async def test_a_truncated_monthly_archive_is_refused_instead_of_returning_a_gap(
    archive_server, monkeypatch
):
    """A PUBLISHED monthly archive that misses days of its own month is a HOLE.

    The daily path catches an absent daily FILE.  Nothing caught a day
    missing from INSIDE a monthly archive: the frame was handed back with
    the gap in it, and the backtest traded it as if it were complete -- a
    P&L that looks valid and is not.  A wrong number is worse than no
    number, so the load must fail.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    # A REAL Binance January with the 19th removed from it.
    truncated, dropped = _truncate_monthly_archive(
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
        drop_days={_epoch_day(datetime(2024, 1, 19, tzinfo=timezone.utc))},
    )
    assert dropped == 24, "the 19th should hold 24 hourly bars"
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        truncated,
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

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
async def test_a_monthly_archive_that_stops_early_is_not_a_hole(
    archive_server, monkeypatch
):
    """The symbol was delisted on the 10th: the days AFTER it are not a hole.

    The same rule the daily path uses: a day before the first observed bar
    is "not listed yet" and a day after the last observed bar is "delisted",
    and neither is a hole.  This is the boundary the monthly check must not
    cross.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)

    truncated, _ = _truncate_monthly_archive(
        _fixture("BTCUSDT-1h-2024-01-futures-um.zip"),
        drop_days={
            _epoch_day(datetime(2024, 1, day, tzinfo=timezone.utc))
            for day in range(11, 32)
        },
    )
    archive_server.add(
        _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1),
        truncated,
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    # The shorter history is served: there is no hole, only a delisting.
    assert frame is not None
    assert len(frame) == 24 * 10
    assert frame.index[-1] == pd.Timestamp("2024-01-10T23:00:00Z")


# ==============================================================================
# R6: the fallback fails FAST, and never serves what it collected on the way
# ==============================================================================


@pytest.mark.asyncio
async def test_an_unreachable_archive_fails_fast_instead_of_stalling(monkeypatch):
    """One unreachable host must not cost the loader MINUTES.

    A single archive request can burn a whole request timeout, and the
    trailing month of a window can need 31 daily files, so without a
    deadline the fallback stalls for minutes before it gives up.  With the
    deadline the whole load gives up inside its budget -- and it gives up
    LOUDLY: None, not a half-filled frame.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)
    monkeypatch.setattr(config, "BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS", 0.5)

    session = _NeverAnswersSession()
    monkeypatch.setattr(bulk_archive, "http_session", session)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    began = time.monotonic()
    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )
    elapsed = time.monotonic() - began

    # It failed LOUDLY: None, not a frame with a hole in it.
    assert result is None
    # And it failed FAST: the whole budget, not one timeout per file.
    assert elapsed < 10, f"the fallback took {elapsed:.1f}s"
    assert len(session.requests) <= 3, session.requests


@pytest.mark.asyncio
async def test_the_deadline_never_serves_the_candles_it_collected_first(monkeypatch):
    """January loaded and February hung: the load must FAIL, not serve January.

    Running out of time is a failure, not a reason to hand back the part
    that happened to arrive: a half-covered window is a wrong number.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)
    monkeypatch.setattr(config, "BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS", 1.0)

    january_url = _monthly_url("BTCUSDT", "1h", FUTURES, 2024, 1)
    session = _NeverAnswersSession(
        served={january_url: _fixture("BTCUSDT-1h-2024-01-futures-um.zip")}
    )
    monkeypatch.setattr(bulk_archive, "http_session", session)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=timezone.utc)

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    # January's candles WERE collected, and none of them are served.
    assert january_url in session.requests
    assert result is None


# ==============================================================================
# R7: what "no data" means on each path
# ==============================================================================


@pytest.mark.asyncio
async def test_the_two_paths_report_no_data_as_the_contract_says(
    archive_server, monkeypatch
):
    """``None`` means "no data loaded"; an empty frame means "loaded, but empty".

    The two paths report "no data" differently because they are different
    situations, and both are correct:

    * the REST path ANSWERED and had no klines for the window -> the load
      succeeded and the window is simply empty -> an empty frame;
    * the archive path is only reached after the REST path was BLOCKED
      (HTTP 451).  An archive with nothing in it means the load produced no
      candles at all -> ``None``, so the hard stops fire.

    Neither path ever hands back a frame with a hole in it.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)

    # The REST path answered, and it had no klines for the window.
    monkeypatch.setattr(data_loader, "_make_api_request", lambda *a, **k: [])
    rest_result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )
    assert rest_result is not None
    assert rest_result.empty

    # The REST path was blocked and the archive had nothing either.
    monkeypatch.setattr(data_loader, "_make_api_request", _blocked_rest_api)
    archive_result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )
    assert archive_result is None


# ==============================================================================
# R9: the calendar bucket size comes from the INTERVAL
# ==============================================================================


def test_a_multi_day_interval_does_not_demand_a_bar_on_every_calendar_day():
    """A "3d" interval gets three-day buckets, not one per calendar day.

    Before, every interval that was not "1M" or "1w" fell through to a
    single calendar day, so a multi-day interval demanded a bar on EVERY
    calendar day and reported holes that were not holes.
    """
    day = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end_of_january = datetime(2024, 2, 1, tzinfo=timezone.utc)
    one_day_buckets = set()
    three_day_buckets = set()
    while day < end_of_january:
        one_day_buckets.add(bulk_archive._bucket_bounds(day, "1d")[0])
        three_day_buckets.add(bulk_archive._bucket_bounds(day, "3d")[0])
        day += timedelta(days=1)

    # 31 calendar days, but only 11 three-day buckets.
    assert len(one_day_buckets) == 31
    assert len(three_day_buckets) == 11

    # And every three-day bucket is three days long.
    start, end = bulk_archive._bucket_bounds(
        datetime(2024, 1, 5, tzinfo=timezone.utc), "3d"
    )
    assert end - start == timedelta(days=3)


def test_the_product_intervals_keep_the_calendar_day_bucket():
    """No supported timeframe changes behaviour."""
    day = datetime(2024, 1, 15, 13, 30, tzinfo=timezone.utc)

    for interval in ("1m", "5m", "15m", "1h", "4h", "1d"):
        start, end = bulk_archive._bucket_bounds(day, interval)
        assert start == datetime(2024, 1, 15, tzinfo=timezone.utc), interval
        assert end == datetime(2024, 1, 16, tzinfo=timezone.utc), interval

    # "1w" is a Monday-anchored week and "1M" is the calendar month.
    start, end = bulk_archive._bucket_bounds(day, "1w")
    assert start == datetime(2024, 1, 15, tzinfo=timezone.utc)  # a Monday
    assert end == datetime(2024, 1, 22, tzinfo=timezone.utc)
    start, end = bulk_archive._bucket_bounds(day, "1M")
    assert start == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert end == datetime(2024, 2, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_multi_day_interval_loads_without_a_false_coverage_error(
    archive_server,
):
    """A "3d" interval must not be reported as full of holes.

    One bar every three days IS a complete month for a three-day
    interval.  With the calendar-day bucket it looked like thirty missing
    days and the load failed for no reason at all.
    """
    rows = _make_rows(datetime(2024, 1, 1, tzinfo=timezone.utc), 11, 3 * 24 * 60)
    archive_server.add(
        _monthly_url("BTCUSDT", "3d", FUTURES, 2024, 1),
        _make_zip(_rows_to_csv(rows), "BTCUSDT-3d-2024-01.csv"),
    )

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "3d", start, end, FUTURES
    )

    assert frame is not None
    assert len(frame) == 11
    assert frame.index[0] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert frame.index[-1] == pd.Timestamp("2024-01-31T00:00:00Z")
