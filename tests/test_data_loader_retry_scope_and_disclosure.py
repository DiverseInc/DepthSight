# tests/test_data_loader_retry_scope_and_disclosure.py
"""Two defects, one failure in different clothes.

* A Binance geo-block (HTTP 451) gave up the RETRY BUDGET of two
  endpoints that have NO fallback to get to.  The give-up belongs to the
  klines path alone, because the klines path is the one with the
  bulk-archive fallback: on aggTrades and on the open-interest history
  the five tries were the only chance to get through a block that
  flaps.

* The coverage note -- "the window you got is not the window you asked
  for" -- was computed by the loader and read by TWO hand-picked call
  sites.  Every other consumer of a loader frame (the trainer, the data
  consumer, the diagnostics routes, the API, the celery paths) could
  receive a shortened, hole-riddled archive frame with nothing at all
  saying so.  The note has to TRAVEL WITH THE FRAME.

NO NETWORK IS USED HERE.  The archives are built locally as real
``.zip`` files through the SAME helpers the rest of the suite uses, and
the HTTP fetch is stubbed.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import backoff._sync
import pandas as pd
import pytest
import requests

from bot_module import config, data_loader, trainer

# The fixture builders of the main suite are reused: a synthetic archive
# that does not look like Binance's would be a fixture that can invent a
# defect, which has already cost this project one false blocker.
from tests.test_data_loader_bulk_archive import (  # noqa: F401
    _CountingSession,
    _fixture,
    _make_rows,
    _monthly_url,
    archive_server,
)
from tests.test_data_loader_bulk_archive_coverage import (  # noqa: F401
    frozen_loader_clock,
)
from tests.test_data_loader_bulk_archive_honesty import (  # noqa: F401
    BINANCE_GEO_BLOCK_BODY,
    PROXY_GEO_BLOCK_BODY,
    _binance_blocked_rest_api,
)

FUTURES = "futures_usdtm"

# The repository root, so the wiring of the celery task can be checked in
# the source: the task cannot be imported without a live database.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The suite's autouse fixture replaces ``data_loader._make_api_request``
# with a stub so that no test can make a real network call
# (tests/conftest.py:738).  These tests are about the RETRY POLICY, so
# they need the REAL one: the original is captured here at import time,
# because module import happens during collection, before any fixture
# runs.
_REAL_MAKE_API_REQUEST = data_loader._make_api_request


class _LogCapture(logging.Handler):
    """Captures the records of ONE logger, whatever the propagation is.

    The suite's logging setup turns propagation OFF for the "bot_module"
    logger (bot_module/logger_setup.py:53), so pytest's ``caplog`` never
    sees these records.  The disclosure has to be FOUND in a log, so the
    test puts a handler on the logger itself.
    """

    def __init__(self, logger):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.logger = logger
        logger.addHandler(self)

    def emit(self, record):
        self.records.append(record)

    def close(self):
        self.logger.removeHandler(self)
        super().close()

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


@pytest.fixture
def no_backoff_sleep(monkeypatch):
    """The retry loop's WAITS are removed: this test counts ATTEMPTS.

    The policy under test is the number of tries, not the number of
    seconds; a 60 second budget is asserted by the decorator's own
    arguments.  Without this the five tries would spend up to 15
    seconds asleep between them.
    """
    monkeypatch.setattr(backoff._sync.time, "sleep", lambda seconds: None)


def _frame_with_a_short_coverage(bars=48, trailing_short=3600.0):
    """A frame that carries the records of a frame shorter than its window."""
    requested_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    requested_end = datetime(2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    rows = _make_rows(requested_start, bars, 60)
    frame = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
    ).astype("float64")
    frame.index = pd.DatetimeIndex(
        [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows],
        name="open_time",
    )
    difference = {
        "source": "bulk_archive",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "interval": "1h",
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": frame.index[0].isoformat(),
        "actual_end": frame.index[-1].isoformat(),
        "rows": len(frame),
        "closed_bars_only": True,
        "trailing_edge_short_by_seconds": trailing_short,
        "leading_shortfall": None,
        "missing_buckets": [],
        "unfetchable_files": [],
        "differs_from_rest": True,
    }
    frame.attrs[data_loader.DIFFERENCE_ATTR] = difference
    frame.attrs[data_loader.PROVENANCE_ATTR] = {
        "source": "bulk_archive",
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": frame.index[0].isoformat(),
        "actual_end": frame.index[-1].isoformat(),
        "rows": len(frame),
        "differs_from_rest": True,
        "interval": "1h",
        "difference": difference,
    }
    return frame


# ==============================================================================
# E1: the geo-block give-up belongs to the path that has a fallback
# ==============================================================================


def test_the_geo_block_giveup_belongs_to_the_klines_path_alone(
    monkeypatch, no_backoff_sleep
):
    """A 451 gives the klines path ONE request and keeps the five tries of
    the paths that have no fallback to get to.

    MEASURED before this change: the give-up sat on the SHARED request
    helper, so a Binance geo-block on the aggTrades or the open-interest
    path gave up after ONE request -- the five tries (and the 60 second
    budget) those two endpoints have always had were gone.  On the
    klines path the give-up is right: the bulk archive answers the 451,
    so the four extra attempts only delayed the fallback that does work.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _REAL_MAKE_API_REQUEST)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)

    # THE KLINES PATH: one attempt, because the archive answers the 451.
    blocked = _CountingSession(451, BINANCE_GEO_BLOCK_BODY)
    monkeypatch.setattr(data_loader, "http_session", blocked)
    with pytest.raises(requests.exceptions.HTTPError):
        data_loader._download_klines_from_api("BTCUSDT", "1h", start, end, FUTURES)
    assert blocked.calls == 1, (
        "the klines path has the bulk-archive fallback, so a geo-block must "
        f"not be retried: it made {blocked.calls} requests"
    )

    # THE AGG-TRADES PATH: five attempts, because there is NO fallback.
    blocked = _CountingSession(451, BINANCE_GEO_BLOCK_BODY)
    monkeypatch.setattr(data_loader, "http_session", blocked)
    with pytest.raises(requests.exceptions.HTTPError):
        data_loader._download_agg_trades_from_api("BTCUSDT", start, end, FUTURES)
    assert blocked.calls == 5, (
        "the aggTrades path has NO archive fallback, so its five tries are "
        f"the only chance to get through a flapping block: it made "
        f"{blocked.calls} requests"
    )

    # THE OPEN-INTEREST PATH: five attempts, for the same reason.
    blocked = _CountingSession(451, BINANCE_GEO_BLOCK_BODY)
    monkeypatch.setattr(data_loader, "http_session", blocked)
    with pytest.raises(requests.exceptions.HTTPError):
        data_loader._download_open_interest_from_api(
            "BTCUSDT", "5m", start, end, FUTURES
        )
    assert blocked.calls == 5, (
        "the open-interest path has NO archive fallback, so its five tries "
        f"are the only chance to get through a flapping block: it made "
        f"{blocked.calls} requests"
    )


def test_a_451_that_is_not_binances_is_re_raised_unchanged_on_every_path(
    monkeypatch, no_backoff_sleep
):
    """A proxy's 451 is not Binance's, and it is re-raised unchanged.

    HTTP 451 is "Unavailable For Legal Reasons" (RFC 7725) and a
    corporate proxy or a WAF answers with it for its OWN reasons.  The
    body is what tells the two apart, and only Binance's own body arms
    the archive.  Every path keeps that rule, and every path keeps its
    five tries for a block that is not Binance's to answer.
    """
    monkeypatch.setattr(data_loader, "_make_api_request", _REAL_MAKE_API_REQUEST)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)

    paths = {
        "klines": lambda: data_loader._download_klines_from_api(
            "BTCUSDT", "1h", start, end, FUTURES
        ),
        "aggTrades": lambda: data_loader._download_agg_trades_from_api(
            "BTCUSDT", start, end, FUTURES
        ),
        "openInterestHist": lambda: data_loader._download_open_interest_from_api(
            "BTCUSDT", "5m", start, end, FUTURES
        ),
    }

    for name, call in paths.items():
        blocked = _CountingSession(451, PROXY_GEO_BLOCK_BODY)
        monkeypatch.setattr(data_loader, "http_session", blocked)
        with pytest.raises(requests.exceptions.HTTPError) as excinfo:
            call()
        response = excinfo.value.response
        assert response is not None
        assert response.status_code == 451
        assert response.text == PROXY_GEO_BLOCK_BODY, (
            f"the {name} path re-raised a DIFFERENT body: a proxy's 451 must "
            "be re-raised unchanged"
        )
        assert blocked.calls == 5, (
            f"the {name} path gave up on a 451 that is not Binance's: it "
            f"made {blocked.calls} requests"
        )


# ==============================================================================
# E2: the disclosure travels with the frame
# ==============================================================================


@pytest.mark.asyncio
async def test_the_loader_attaches_the_coverage_note_to_the_frame_it_returns(
    archive_server, monkeypatch, tmp_path, frozen_loader_clock
):
    """The frame the loader returns CARRIES the note.

    The note used to be computed by the loader and read by two
    hand-picked call sites, so every other consumer of a loader frame
    could receive a shortened, hole-riddled archive frame with nothing
    saying so.  The note has to be ON the frame, at the point the frame
    is built, so every consumer of it gets the disclosure for free.
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

    assert result is not None, "the archive must answer a blocked klines load"

    assert "klines_provenance" in result.attrs, (
        "the provenance must stay reachable FROM THE FRAME the loader "
        "returns: it is what says where the candles came from"
    )
    assert "klines_window_coverage" in result.attrs, (
        "the coverage note must travel WITH the frame: it is the only thing "
        "that tells a consumer that the window it got is not the window it "
        "asked for"
    )
    note = result.attrs["klines_window_coverage"]
    assert note["source"] == "bulk_archive"
    assert note["trailing_edge_short_by_seconds"] > 0
    assert note["requested_end"] == "2024-02-02T23:59:59.999999+00:00"

    # AND THE LOADER'S OWN READER SEES THE SAME THING.
    assert data_loader.klines_coverage_note(result) == note
    assert data_loader.klines_window_coverage(result) == note


def test_the_coverage_note_is_attached_on_every_path_the_loader_returns(
    monkeypatch, tmp_path
):
    """The note is attached where the frame is built, on EVERY path.

    Three paths return a frame: the complete local cache, the cache
    served when the remote failed, and the merged frame.  Each of them
    has to carry the note, or a caller receives a frame nobody
    disclosed.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)

    # A frame that carries the provenance of a frame that is short.
    frame = _frame_with_a_short_coverage()
    data_loader._attach_klines_coverage_note(frame)
    assert data_loader.klines_coverage_note(frame) is not None
    assert "klines_window_coverage" in frame.attrs

    # A frame with NO provenance carries no note: nothing was measured, so
    # nothing is claimed.
    plain = pd.DataFrame({"close": [1.0]})
    assert data_loader.klines_coverage_note(plain) is None
    assert data_loader.klines_coverage_note(None) is None


@pytest.mark.asyncio
async def test_the_cache_only_frame_carries_the_note_too(
    archive_server, monkeypatch, tmp_path, frozen_loader_clock
):
    """The frame served from the cache alone carries the note as well.

    When the remote source fails and the loader serves the cached
    history only, the frame covers a SHORTER window than the one that
    was asked for -- and that is exactly the frame a caller must not
    mistake for the window it asked for.
    """
    monkeypatch.setattr(config, "LOCAL_DATA_STORAGE_PATH", str(tmp_path))
    frozen_loader_clock(datetime(2024, 2, 2, 12, 0, tzinfo=timezone.utc))

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 1, tzinfo=timezone.utc)

    rows = _make_rows(start, 300, 60)
    cached = pd.DataFrame(
        [row[1:6] for row in rows],
        columns=["open", "high", "low", "close", "volume"],
    ).astype("float64")
    cached.index = pd.DatetimeIndex(
        [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows],
        name="open_time",
    )
    local_path = (
        Path(tmp_path) / "binance" / "futures" / "BTCUSDT" / "kline_1h.parquet"
    )
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cached.to_parquet(local_path)

    # THE REMOTE SOURCE FAILED (geo-blocked) and the archive holds
    # nothing: the frame that is about to be returned is the CACHE's,
    # and the provenance must say so.
    monkeypatch.setattr(data_loader, "_make_api_request", _binance_blocked_rest_api)

    result = await data_loader.download_klines(
        symbol="BTCUSDT",
        timeframe="1h",
        start_dt=start,
        end_dt=end,
        market_type=FUTURES,
    )

    assert result is not None, "the cache must still be served when the remote fails"
    assert result.attrs[data_loader.SOURCE_ATTR] == "local_cache"
    assert "klines_window_coverage" in result.attrs, (
        "the cache-only frame must carry the note: it does not cover the "
        "window that was asked for"
    )
    note = data_loader.klines_coverage_note(result)
    assert note is not None
    assert note["remote_failed"] is True


def test_the_trainer_logs_the_window_it_did_not_get(monkeypatch):
    """A shortened TRAINING window is visible in the log, under a marker
    of its own.

    A model trained on a silently shortened, hole-riddled archive frame
    is a model whose training data nobody can account for.  The
    disclosure has to be greppable, not swallowed by a generic message.
    """
    frame = _frame_with_a_short_coverage()
    note = data_loader.klines_window_coverage(frame)
    assert note is not None, "the fixture must carry a coverage note"
    frame.attrs["klines_window_coverage"] = note

    monkeypatch.setattr(trainer, "download_klines", lambda *args, **kwargs: frame)

    capture = _LogCapture(data_loader.logger)
    try:
        trainer._load_data_for_process(
            "BTCUSDT",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 2, tzinfo=timezone.utc),
            0,
            {"kline_1h"},
            False,
            Path("."),
        )
    finally:
        capture.close()

    assert data_loader.COVERAGE_LOG_MARKER in capture.text, (
        "the trainer must log the window it did not get, under a marker of "
        f"its own: the log said {capture.text!r}"
    )


@pytest.mark.asyncio
async def test_the_trainer_class_path_logs_the_window_it_did_not_get(monkeypatch):
    """The OTHER trainer path discloses the same thing.

    The trainer loads its frames in two places; a disclosure that only
    reaches one of them is a disclosure that reaches the one that
    happens to be used.
    """
    frame = _frame_with_a_short_coverage()
    note = data_loader.klines_window_coverage(frame)
    assert note is not None
    frame.attrs["klines_window_coverage"] = note

    async def fake_download_klines(*args, **kwargs):
        return frame

    monkeypatch.setattr(trainer, "download_klines", fake_download_klines)

    stub = SimpleNamespace(local_data_path=Path("."))
    capture = _LogCapture(data_loader.logger)
    try:
        await trainer.Trainer._load_historical_data(
            stub,
            "BTCUSDT",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 2, tzinfo=timezone.utc),
            {"kline_1h"},
        )
    finally:
        capture.close()

    assert data_loader.COVERAGE_LOG_MARKER in capture.text, (
        "the trainer must log the window it did not get, under a marker of "
        f"its own: the log said {capture.text!r}"
    )


# ==============================================================================
# E3: the enumeration -- no consumer is left without the disclosure
# ==============================================================================

PRODUCT_CALLER_FILES = (
    "api/depthsight_api.py",
    "api/routes/backtests.py",
    "api/routes/diagnostics.py",
    "bot_module/data_consumer.py",
    "bot_module/portfolio_backtester.py",
    "bot_module/trainer.py",
    "tasks.py",
)

# The loader's PRIVATE download functions.  A product file that calls one
# of them directly gets a frame the loader never disclosed.
PRIVATE_LOADER_FUNCTIONS = (
    "_download_klines_from_api",
    "_download_klines_from_api_with_archive_fallback",
    "fetch_klines_from_bulk_archive",
)


def test_no_product_caller_bypasses_the_frame_the_loader_discloses():
    """ENUMERATION: every consumer of klines goes through the loader's
    PUBLIC entry point.

    The disclosure is attached at the frame boundary, so it reaches every
    consumer -- but only if every consumer goes through that boundary.
    A product file that called a private download function would get a
    frame nobody disclosed, and that is what this test looks for.
    """
    call_sites = []
    for rel in PRODUCT_CALLER_FILES:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for private in PRIVATE_LOADER_FUNCTIONS:
            assert private not in source, (
                f"{rel} calls {private} directly: it bypasses the frame the "
                f"loader discloses"
            )
        for number, line in enumerate(source.splitlines(), start=1):
            if "download_klines(" in line:
                call_sites.append(f"{rel}:{number}")

    assert len(call_sites) >= 10, (
        "the loader has ten product call sites and every one of them has "
        f"to reach the frame boundary; the enumeration found {len(call_sites)}: "
        f"{call_sites}"
    )


def test_the_kpi_payload_still_carries_the_window_it_was_computed_over():
    """THE EXISTING BEHAVIOUR IS UNCHANGED.

    The KPI dict a run saves still carries the coverage under
    ``data_window``, with the same shape, and the frame's own note is
    the same note.
    """
    frame = _frame_with_a_short_coverage()
    kpis = {"total_pnl": 12.5}

    attached = data_loader.attach_klines_window_coverage(kpis, {"kline_1h": frame})

    assert attached is not None
    assert kpis["data_window"][0]["data_key"] == "kline_1h"
    assert kpis["data_window"][0]["source"] == "bulk_archive"
    assert kpis["total_pnl"] == 12.5
