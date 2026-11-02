# tests/test_data_loader_bulk_archive_coverage.py
"""A frame must be CHECKED for its coverage, and its coverage must REACH the
caller.

The bulk-archive fallback can answer with a frame that is SHORTER than the
window that was asked for.  The loader records that -- in the provenance and
in the difference record -- but those records used to be read by NOBODY
outside the archive module, so a backtest could report the KPIs of a shorter
window as the KPIs of the window it was asked for.

Four defects, one failure in different clothes:

* E1  a frame that is short for a reason NOTHING explains -- a window that
     ended long enough ago for Binance to have published every one of its
     days -- is REFUSED, not served;
* E2  a frame that IS short, for the one reason that is legitimate (Binance
     had not published those days yet), is served, and the caller can TELL,
     because the coverage travels with the KPIs;
* E3  the local cache is filed under the venue the candles came from, so the
     COIN-M alias is not filed under the SPOT folder;
* E4  ONE file that cannot be fetched does not collapse the budget of the
     load; a SECOND one does.

NO NETWORK IS USED HERE.  The archives are built locally as real ``.zip``
files containing a real CSV -- through the SAME helpers the rest of the
suite uses -- and the HTTP fetch is stubbed.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import requests

from bot_module import bulk_archive, config, data_loader

# The repository root, so the wiring of the celery task can be checked in the
# source: the task cannot be imported without a live database.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The fixture builders of the main suite are reused: a synthetic archive
# that does not look like Binance's would be a fixture that can invent a
# defect.
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
from tests.test_data_loader_bulk_archive_honesty import (  # noqa: F401
    _binance_blocked_rest_api,
    _frozen_clock,
)

FUTURES = "futures_usdtm"
SPOT = "spot"
COINM = "futures_coinm"


@pytest.fixture
def frozen_loader_clock(monkeypatch):
    """Installs a pinned clock into the LOADER module and returns a setter."""

    def _set(moment: datetime):
        monkeypatch.setattr(data_loader, "datetime", _frozen_clock(moment))
        return moment

    return _set


class _FlakyArchiveServer(FakeArchiveServer):
    """Answers every file, except the ones it was told it cannot reach.

    The failures are TRANSPORT failures -- the connection was reset -- which
    is what a CDN edge or a changing network looks like from the loader.
    """

    def __init__(self, failing_urls=()):
        super().__init__()
        self.failing_urls = set(failing_urls)

    def fetch(self, url):
        self.requested.append(url)
        if url in self.failing_urls:
            raise requests.exceptions.ConnectionError(
                "the connection was reset by the peer"
            )
        return self.files.get(url)


@pytest.fixture
def flaky_archive_server(monkeypatch):
    """A server whose failures are the caller's to configure."""
    server = _FlakyArchiveServer()
    monkeypatch.setattr(bulk_archive, "fetch_archive_bytes", server.fetch)
    return server


# ==============================================================================
# E1: a frame that is short for no reason is refused
# ==============================================================================


@pytest.mark.asyncio
async def test_a_frame_that_is_short_only_because_the_archive_does_not_hold_it_is_served(
    archive_server, monkeypatch, tmp_path, frozen_loader_clock
):
    """A short frame is SERVED, and the caller is TOLD.

    The archive holds closed bars only, it lags the REST path by the
    publication lag, and it can be missing days the REST path has.  The
    contract this loader already has is "the data that EXISTS is worth more
    than no data": the frame is served and the gap is NAMED.  What was
    missing was the delivery -- the records were read by nobody outside this
    module -- so the coverage travels with the KPIs now.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)
    frozen_loader_clock(datetime(2024, 2, 2, 12, 0, tzinfo=timezone.utc))

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 2, 23, 59, 59, 999999, tzinfo=timezone.utc)

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

    assert result is not None, (
        "a frame that is short because the archive does not hold those days "
        "must be served: the data that exists is worth more than no data"
    )

    coverage = data_loader.klines_window_coverage(result)
    assert coverage is not None, (
        "the coverage of a frame that is shorter than the window must be "
        "readable by the caller, not only by the loader"
    )
    assert coverage["trailing_edge_short_by_seconds"] > 0
    assert coverage["requested_end"] == "2024-02-02T23:59:59.999999+00:00"
    assert coverage["actual_end"] == "2024-01-31T23:00:00+00:00"
    assert coverage["source"] == "bulk_archive"


def test_a_frame_that_covers_the_window_has_no_coverage_to_report():
    """Nothing to say is the same thing as saying nothing."""
    frame = pd.DataFrame(
        {
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
        },
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
    )
    assert data_loader.klines_window_coverage(frame) is None
    assert data_loader.klines_window_coverage(None) is None


# ==============================================================================
# E2: the coverage reaches the KPIs
# ==============================================================================


class _FakeBacktester:
    """A backtester that returns fixed KPIs."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def run(self):
        return {"trades": 3, "total_pnl": 12.5, "sharpe_ratio": 1.2}


def _frame_with_a_short_coverage():
    """A frame that carries the records of a frame shorter than its window."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(start, 24, 60)
    frame = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
    ).astype("float64")
    frame.index = pd.DatetimeIndex(
        [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows],
        name="open_time",
    )
    frame.attrs[data_loader.DIFFERENCE_ATTR] = {
        "source": "bulk_archive",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "market_type": FUTURES,
        "interval": "1h",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "actual_start": start.isoformat(),
        "actual_end": "2024-01-31T23:00:00+00:00",
        "rows": 24,
        "closed_bars_only": True,
        "trailing_edge_short_by_seconds": 2 * 24 * 3600,
        "publication_lag_days": 2.0,
        "bar_grid_phase_hours": 0.0,
        "day_boundary_shift": None,
        "leading_shortfall": None,
        "missing_buckets": [],
        "unfetchable_files": [],
        "differs_from_rest": True,
        "reason": "the archive holds only CLOSED bars",
    }
    frame.attrs[data_loader.PROVENANCE_ATTR] = {
        "source": "bulk_archive",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "actual_start": start.isoformat(),
        "actual_end": "2024-01-31T23:00:00+00:00",
        "rows": 24,
        "differs_from_rest": True,
        "difference": frame.attrs[data_loader.DIFFERENCE_ATTR],
    }
    return frame


def test_the_coverage_of_every_loaded_frame_is_collected():
    """Every frame that does not cover its window is reported."""
    frame = _frame_with_a_short_coverage()

    notes = data_loader.klines_window_coverages({"kline_1h": frame})
    assert len(notes) == 1
    assert notes[0]["data_key"] == "kline_1h"
    assert notes[0]["requested_end"] == "2024-01-31T23:59:59.999999+00:00"
    assert notes[0]["actual_end"] == "2024-01-31T23:00:00+00:00"
    assert notes[0]["trailing_edge_short_by_seconds"] == 2 * 24 * 3600
    assert notes[0]["reason"]

    # A frame that covers its window has nothing to report.
    assert data_loader.klines_window_coverages({"kline_1h": None}) == []
    assert data_loader.klines_window_coverages(None) == []


def test_a_frame_that_starts_later_than_the_window_is_reported():
    """A leading gap is measured, not taken from the archive's own record.

    The archive only records a leading shortfall when it can PROVE the
    symbol was listed later than the window asks for.  A leading month whose
    monthly archive is PUBLISHED but TRUNCATED leaves no record at all, and
    the frame then starts later than the window with nobody saying so -- so
    the delivered window and the requested one are compared directly.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = _make_rows(start + timedelta(days=3), 24, 60)
    frame = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
    ).astype("float64")
    frame.index = pd.DatetimeIndex(
        [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows],
        name="open_time",
    )
    frame.attrs[data_loader.PROVENANCE_ATTR] = {
        "source": "bulk_archive",
        "requested_start": start.isoformat(),
        "requested_end": "2024-01-31T23:59:59.999999+00:00",
        "actual_start": (start + timedelta(days=3)).isoformat(),
        "actual_end": "2024-01-04T23:00:00+00:00",
        "rows": 24,
        "differs_from_rest": True,
        "difference": {
            "source": "bulk_archive",
            "trailing_edge_short_by_seconds": 0.0,
            # The archive's own record says NOTHING about the leading gap:
            # the month was published, so it could not prove a listing.
            "leading_shortfall": None,
            "missing_buckets": [],
        },
    }

    coverage = data_loader.klines_window_coverage(frame)
    assert coverage is not None, (
        "a frame that starts later than the window that was asked for must be "
        "reported, even when the archive's own record says nothing about it"
    )
    assert coverage["leading_gap_seconds"] == 3 * 24 * 3600
    assert coverage["actual_start"] == "2024-01-04T00:00:00+00:00"
    assert coverage["requested_start"] == "2024-01-01T00:00:00+00:00"
    assert "after the requested start" in coverage["reason"]


def test_a_gap_the_archive_never_saw_is_not_blamed_on_the_archive():
    """The note names the HOLDER of the missing days, and the holder is the source.

    A REST frame that starts late is BINANCE's own answer -- a symbol that
    was listed after the window that was asked for, for instance -- and the
    archive was never consulted for it.  Blaming the archive for those days
    was a false statement in the note the caller carries, and the note is
    what the user is shown.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = _make_rows(start + timedelta(days=3), 24, 60)
    frame = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
    ).astype("float64")
    frame.index = pd.DatetimeIndex(
        [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows],
        name="open_time",
    )
    # A REST frame carries NO difference record: the archive was not asked.
    frame.attrs[data_loader.PROVENANCE_ATTR] = {
        "source": "rest_api",
        "requested_start": start.isoformat(),
        "requested_end": "2024-01-31T23:59:59.999999+00:00",
        "actual_start": (start + timedelta(days=3)).isoformat(),
        "actual_end": "2024-01-04T23:00:00+00:00",
        "rows": 24,
        "differs_from_rest": False,
    }

    coverage = data_loader.klines_window_coverage(frame)

    assert coverage is not None
    assert coverage["source"] == "rest_api"
    assert coverage["leading_gap_seconds"] == 3 * 24 * 3600
    assert "the archive" not in coverage["reason"], coverage["reason"]
    assert "the Binance REST API" in coverage["reason"], coverage["reason"]

    # And the archive IS the holder when the archive is the source.
    archive_note = data_loader.klines_window_coverage(_frame_with_a_short_coverage())
    assert archive_note["source"] == "bulk_archive"
    assert "the archive" in archive_note["reason"], archive_note["reason"]


def test_the_coverage_is_put_on_the_kpis_the_run_saves():
    """The note the loader keeps ends up on the KPI dict."""
    frame = _frame_with_a_short_coverage()
    kpis = {"total_pnl": 12.5, "sharpe_ratio": 1.2}

    attached = data_loader.attach_klines_window_coverage(
        kpis, {"kline_1h": frame}
    )

    assert attached is not None
    assert kpis["data_window"][0]["data_key"] == "kline_1h"
    # The KPIs the run already had are untouched.
    assert kpis["total_pnl"] == 12.5
    assert kpis["sharpe_ratio"] == 1.2

    # Nothing to report means nothing is added.
    plain = {"total_pnl": 1.0}
    assert data_loader.attach_klines_window_coverage(plain, {}) is None
    assert "data_window" not in plain
    assert data_loader.attach_klines_window_coverage(plain, None) is None
    assert "data_window" not in plain


def test_both_kpi_producers_carry_the_coverage():
    """The two places that SAVE KPIs both carry the coverage.

    The strategy backtest saves its KPIs in the celery task (``tasks.py``)
    and the portfolio backtest saves them in the portfolio backtester.  The
    celery task cannot be imported without a live database -- it raises at
    import time -- so its wiring is checked in the SOURCE, and the function
    it calls is checked by the test above.  The portfolio backtester is
    driven for real.
    """
    tasks_source = (REPO_ROOT / "tasks.py").read_text(encoding="utf-8")
    assert "attach_klines_window_coverage(" in tasks_source, (
        "the strategy backtest saves its KPIs in tasks.py, and that is where "
        "the coverage of the window they were computed over has to be put"
    )
    assert "kpi_results_to_save" in tasks_source

    portfolio_source = (REPO_ROOT / "bot_module" / "portfolio_backtester.py").read_text(
        encoding="utf-8"
    )
    assert "attach_klines_window_coverage(" in portfolio_source


@pytest.mark.asyncio
async def test_the_portfolio_kpis_carry_the_window_they_were_computed_over():
    """The KPIs say WHICH WINDOW they are about.

    The loader records the coverage; the backtest is what turns it into
    numbers, so the numbers have to carry it.  A record that nobody reads is
    a record that does not happen, and the KPIs of a shorter window would
    be reported as the KPIs of the requested one.
    """
    from unittest.mock import AsyncMock, patch

    from bot_module.portfolio_backtester import PortfolioBacktester
    from bot_module.strategy import (
        STRATEGIES,
        FakeBreakoutStrategy,
        VolumeBreakoutStrategy,
    )

    # Register the strategies the way the portfolio suite does: they are not
    # in the production allow-list.
    STRATEGIES["VolumeBreakout"] = VolumeBreakoutStrategy
    STRATEGIES["FakeBreakout"] = FakeBreakoutStrategy

    frame = _frame_with_a_short_coverage()
    contracts = [
        {
            "id": "BTC_VB_1h",
            "strategy_name": "VolumeBreakout",
            "symbol": "BTCUSDT",
            "market_type": FUTURES,
            "params": {
                "tf": "1h",
                "stop_loss_atr_multiplier": 1.0,
                "take_profit_atr_multiplier": 1.5,
                "atr_period": 14,
                "enabled": True,
            },
        }
    ]

    with patch(
        "bot_module.portfolio_backtester.download_klines", new_callable=AsyncMock
    ) as mock_download:
        mock_download.side_effect = lambda symbol, timeframe, **kwargs: frame
        backtester = PortfolioBacktester(
            initial_balance=10000.0,
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 31, tzinfo=timezone.utc),
            contracts=contracts,
            global_risk_limits={
                "max_total_exposure_pct": 2.5,
                "max_concurrent_positions": 1,
                "commission_pct": 0.001,
                "risk_pct_per_trade": 0.01,
            },
        )
        kpis = await backtester.run_backtest()

    assert kpis is not None
    assert "data_window" in kpis, (
        "the KPIs of a backtest must say which window they were computed "
        "over, or a shorter window is reported as the requested one"
    )
    note = kpis["data_window"][0]
    assert note["requested_end"] == "2024-01-31T23:59:59.999999+00:00"
    assert note["actual_end"] == "2024-01-31T23:00:00+00:00"
    assert note["trailing_edge_short_by_seconds"] == 2 * 24 * 3600
    assert note["data_key"] == "BTCUSDT_1h"


# ==============================================================================
# E3: the cache is filed under the venue the candles came from
# ==============================================================================


def test_the_cache_is_filed_under_the_venue_the_candles_came_from(monkeypatch, tmp_path):
    """One venue predicate, asked in one place.

    The cache folder used to be decided by a SECOND, hand-written copy of
    the venue predicate, and the two copies disagreed: the "coinm" alias was
    filed under the SPOT folder while the REST path and the archive answered
    it with COIN-M candles.  A venue predicate that is duplicated must
    handle the same special cases in every copy.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))

    for market_type, venue in (
        ("spot", "spot"),
        ("futures_usdtm", "futures_usdtm"),
        ("futures_coinm", "futures_coinm"),
        ("futures_cm", "futures_coinm"),
        ("coinm", "futures_coinm"),
    ):
        resolved = config.resolve_market_venue(market_type)
        assert resolved == venue, market_type
        path = data_loader.get_local_path("BTCUSDT", "klines", market_type, "1h")
        expected_folder = "spot" if resolved == "spot" else "futures"
        assert path.parent.parent.name == expected_folder, (
            f"{market_type} resolves to the {resolved} venue, so its cache must "
            f"live in the {expected_folder} folder, not in {path.parent.parent.name}"
        )


# ==============================================================================
# E4: one file that cannot be fetched does not collapse the load
# ==============================================================================


@pytest.mark.asyncio
async def test_one_file_that_cannot_be_fetched_does_not_collapse_the_load(
    flaky_archive_server, monkeypatch
):
    """One unlucky file must not throw away the whole load.

    A transport failure is not proof that the host is gone: a CDN edge that
    resets one connection, a network that changes, or a proxy that drops one
    request all look the same from here.  The load keeps its budget, the day
    it could not read is counted as absent, and the file is RECORDED -- a
    file that could not be fetched is a different fact from a file Binance
    has not published, and the record has to tell them apart.
    """
    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        flaky_archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )

    broken_url = _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 19)
    flaky_archive_server.failing_urls.add(broken_url)

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    frame = bulk_archive.fetch_klines_from_bulk_archive(
        "BTCUSDT", "1h", start, end, FUTURES
    )

    assert frame is not None, (
        "one file that cannot be fetched must not end the load: the days "
        "that CAN be fetched are worth more than no days at all"
    )
    # The four whole days plus the first thirteen hours of the 20th: the
    # 19th is the gap.
    assert len(frame) == 4 * 24 + 13, len(frame)

    difference = bulk_archive.frame_difference(frame)
    assert broken_url in difference["unfetchable_files"], (
        "a file that could not be FETCHED is a different fact from a file "
        "Binance has not published, and the record has to say which one it is"
    )
    missing = difference["missing_buckets"]
    assert len(missing) == 1, missing
    assert missing[0]["start"].startswith("2024-01-19")


@pytest.mark.asyncio
async def test_a_host_that_is_really_gone_still_ends_the_load_fast(
    flaky_archive_server, monkeypatch
):
    """A SECOND file that cannot be fetched means the host is gone.

    The collapse exists so a load does not spend its whole budget on a host
    that is not answering.  One failure is not enough to prove that, so the
    load asks for the NEXT file: if that one fails too, the host is gone and
    the load ends.
    """
    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        flaky_archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )

    flaky_archive_server.failing_urls.add(
        _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 18)
    )
    flaky_archive_server.failing_urls.add(
        _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 19)
    )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    with pytest.raises(requests.exceptions.RequestException):
        bulk_archive.fetch_klines_from_bulk_archive(
            "BTCUSDT", "1h", start, end, FUTURES
        )

    # The load asked for the file AFTER the first failure: it did not give up
    # on the first one.
    assert _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 19) in (
        flaky_archive_server.requested
    ), flaky_archive_server.requested


def test_a_host_that_does_not_answer_is_told_apart_from_a_lost_request():
    """A TIMEOUT is evidence about the HOST; a RESET is evidence about one request.

    The two look the same to a caller, and they must not look the same to
    the loader: a host that did not answer inside its timeout is not there,
    and the load must not wait for it, while a connection that was reset is
    one unlucky request.
    """
    assert bulk_archive._is_host_not_answering(
        requests.exceptions.ReadTimeout("the host did not answer")
    )
    assert bulk_archive._is_host_not_answering(
        requests.exceptions.ConnectTimeout("the host did not accept")
    )
    assert not bulk_archive._is_host_not_answering(
        requests.exceptions.ConnectionError("the connection was reset")
    )
    assert not bulk_archive._is_host_not_answering(
        requests.exceptions.HTTPError("500 Server Error")
    )


@pytest.mark.asyncio
async def test_a_failed_file_is_logged_loudly(
    flaky_archive_server, monkeypatch, caplog
):
    """The file that could not be fetched is in the log too."""
    for day in (15, 16, 17, 18, 20):
        rows = _make_rows(datetime(2024, 1, day, tzinfo=timezone.utc), 24, 60)
        flaky_archive_server.add(
            _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, day),
            _make_zip(_rows_to_csv(rows), f"BTCUSDT-1h-2024-01-{day:02d}.csv"),
        )
    flaky_archive_server.failing_urls.add(
        _daily_url("BTCUSDT", "1h", FUTURES, 2024, 1, 19)
    )

    start = datetime(2024, 1, 15, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    with caplog.at_level(logging.WARNING, logger="bot_module.bulk_archive"):
        bulk_archive.fetch_klines_from_bulk_archive(
            "BTCUSDT", "1h", start, end, FUTURES
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bot_module.bulk_archive"
    ]
    assert any("could not be fetched" in message for message in messages), messages
