# tests/test_backtest_data_window_disclosure.py
"""The backtest result must TELL the customer when part of the requested
candle window was missing.

The loader knows what it delivered: where the candles came from, which
window they cover and -- when they came from the bulk archive -- which
days inside that window the archive does not hold.  The backtest is what
turns those candles into numbers, so the numbers are what the customer
reads, and a number that was computed over a SHORTER window than the one
that was asked for must say so.

What was missing is the delivery.  The loader recorded the coverage, the
KPI dict carried it, and the API served the KPI dict -- but the disclosure
was a NOTE about the frames that were short, and nothing said where the
candles came from when nothing was short.  A customer who found no note
could not tell "nothing was missing" from "nobody looked", and a result
that said nothing looked exactly like a result about the window that was
asked for.

Four guarantees, one failure in different clothes:

* a backtest over a frame with a gap SAYS SO -- the missing days and the
  fact that the window is incomplete;
* a backtest over a frame with NO gap says the window was COMPLETE, so
  the absence of a warning is a statement and not a silence;
* the SOURCE of the candles (the REST API or the bulk archive) reaches the
  result the customer receives;
* a REST-sourced backtest keeps the numbers it always had.

NO NETWORK IS USED HERE.  The frames are built locally and the loader is
not called: what is under test is what the backtest does with the records
the loader keeps.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pandas as pd

from api import schemas
from bot_module import data_loader
from bot_module.market_data_disclosure import (
    attach_candles_disclosure,
    candles_disclosure,
)
from bot_module.portfolio_backtester import PortfolioBacktester
from bot_module.strategy import (
    STRATEGIES,
    FakeBreakoutStrategy,
    VolumeBreakoutStrategy,
)

# The fixture builders of the main suite are reused: a synthetic frame that
# does not look like the loader's would be a fixture that can invent a
# defect.
from tests.test_data_loader_bulk_archive import _make_rows  # noqa: F401

FUTURES = "futures_usdtm"

# The strategies are not in the production allow-list; the portfolio suite
# registers them the same way.
STRATEGIES["VolumeBreakout"] = VolumeBreakoutStrategy
STRATEGIES["FakeBreakout"] = FakeBreakoutStrategy


def _frame_with_provenance(
    source="bulk_archive",
    requested_start=None,
    requested_end=None,
    bars=24,
    start=None,
    missing_buckets=None,
    trailing_short=0.0,
):
    """A frame that carries the records the loader keeps on it."""
    requested_start = requested_start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    requested_end = requested_end or datetime(
        2024, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc
    )
    start = start or requested_start
    rows = _make_rows(start, bars, 60)
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
        "market_type": FUTURES,
        "interval": "1h",
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": frame.index[0].isoformat(),
        "actual_end": frame.index[-1].isoformat(),
        "rows": len(frame),
        "closed_bars_only": True,
        "trailing_edge_short_by_seconds": trailing_short,
        "publication_lag_days": 2.0,
        "bar_grid_phase_hours": 0.0,
        "day_boundary_shift": None,
        "leading_shortfall": None,
        "missing_buckets": list(missing_buckets or []),
        "unfetchable_files": [],
        "differs_from_rest": True,
        "reason": "the archive holds only CLOSED bars",
    }
    frame.attrs[data_loader.DIFFERENCE_ATTR] = difference
    frame.attrs[data_loader.PROVENANCE_ATTR] = {
        "source": source,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": frame.index[0].isoformat(),
        "actual_end": frame.index[-1].isoformat(),
        "rows": len(frame),
        "differs_from_rest": source != "rest_api",
        "interval": "1h",
        "difference": difference,
    }
    return frame


def _contracts():
    return [
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


async def _run_portfolio_backtest(frame):
    """Runs the portfolio backtester for real over ``frame``."""
    with patch(
        "bot_module.portfolio_backtester.download_klines", new_callable=AsyncMock
    ) as mock_download:
        mock_download.side_effect = lambda symbol, timeframe, **kwargs: frame
        backtester = PortfolioBacktester(
            initial_balance=10000.0,
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 31, tzinfo=timezone.utc),
            contracts=_contracts(),
            global_risk_limits={
                "max_total_exposure_pct": 2.5,
                "max_concurrent_positions": 1,
                "commission_pct": 0.001,
                "risk_pct_per_trade": 0.01,
            },
        )
        return await backtester.run_backtest()


# ==============================================================================
# E1: a backtest over a window with a hole says so
# ==============================================================================


async def test_a_backtest_over_a_frame_with_a_gap_says_so_in_the_result():
    """The result the customer receives says which part of the window is
    missing, and that the window is incomplete.

    The archive holds CLOSED bars only, it lags the REST path by the
    publication lag and it can be missing days the REST path has.  The
    frame is SERVED -- the data that exists is worth more than no data --
    but the result must not read like a result about the window that was
    asked for.
    """
    frame = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
            {"start": "2024-01-17T00:00:00+00:00", "end": "2024-01-18T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )

    kpis = await _run_portfolio_backtest(frame)

    assert kpis is not None
    assert "data_window" in kpis, (
        "the result the customer receives must say which window the numbers "
        "were computed over"
    )
    record = kpis["data_window"][0]
    assert record["complete"] is False, (
        "a frame that does not cover the window that was asked for must be "
        "reported as INCOMPLETE"
    )
    assert record["missing_buckets"], (
        "the days that are missing from the window have to be named: a "
        "warning that says nothing about WHICH days is half a warning"
    )
    assert len(record["missing_buckets"]) == 2
    assert record["missing_buckets"][0]["start"].startswith("2024-01-10")
    assert record["missing_buckets"][1]["start"].startswith("2024-01-17")
    assert record["trailing_edge_short_by_seconds"] == 2 * 24 * 3600
    assert record["reason"], "the record must say WHY the window is incomplete"


async def test_the_gap_is_the_first_thing_the_customer_reads():
    """The incomplete frames come FIRST in the list."""
    complete = _frame_with_provenance(
        source="rest_api",
        bars=24 * 30,
        requested_end=datetime(2024, 1, 30, 23, 0, tzinfo=timezone.utc),
    )
    incomplete = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )

    records = candles_disclosure(
        {"kline_1h": complete, "kline_4h": incomplete},
        [
            {
                "data_key": "kline_4h",
                "source": "bulk_archive",
                "missing_buckets": incomplete.attrs[data_loader.DIFFERENCE_ATTR][
                    "missing_buckets"
                ],
                "trailing_edge_short_by_seconds": 2 * 24 * 3600,
                "reason": "the frame ends early",
            }
        ],
    )

    assert [record["data_key"] for record in records] == ["kline_4h", "kline_1h"]
    assert records[0]["complete"] is False
    assert records[1]["complete"] is True


# ==============================================================================
# E2: a backtest over a window with no gap says the window was COMPLETE
# ==============================================================================


async def test_a_backtest_over_a_complete_window_reports_no_gap():
    """Nothing is missing, and the result says so.

    The absence of a warning has to mean "nothing was missing" and not
    "nobody looked".  A frame that covers the window it was asked for is
    reported as COMPLETE, and it is never reported as a gap.
    """
    frame = _frame_with_provenance(
        source="rest_api",
        bars=24 * 30,
        requested_end=datetime(2024, 1, 30, 23, 0, tzinfo=timezone.utc),
    )

    kpis = await _run_portfolio_backtest(frame)

    assert kpis is not None
    record = kpis["data_window"][0]
    assert record["complete"] is True, (
        "a frame that covers the window that was asked for must be reported "
        "as COMPLETE: the absence of a warning has to be a statement"
    )
    assert not record.get("missing_buckets"), record.get("missing_buckets")
    assert record.get("trailing_edge_short_by_seconds") in (None, 0.0)
    assert record.get("leading_gap_seconds") in (None, 0.0)


async def test_a_frame_that_carries_no_provenance_is_not_invented():
    """A frame that says nothing about itself is left out.

    A record invented for a frame nobody described would be a claim
    nobody checked.
    """
    frame = _frame_with_provenance(source="rest_api")
    frame.attrs.pop(data_loader.PROVENANCE_ATTR)

    records = candles_disclosure({"kline_1h": frame}, [])

    assert records == []


# ==============================================================================
# E3: the SOURCE of the candles reaches the customer
# ==============================================================================


async def test_the_source_of_the_candles_reaches_the_result_the_customer_reads():
    """Where the candles came from is part of what the customer is told.

    The archive's frame is NOT the REST frame, and a result that does not
    say which one it was computed over cannot be judged by the customer.
    """
    archive_frame = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )

    kpis = await _run_portfolio_backtest(archive_frame)

    record = kpis["data_window"][0]
    assert record["source"] == "bulk_archive"
    assert record["differs_from_rest"] is True

    rest_frame = _frame_with_provenance(
        source="rest_api",
        bars=24 * 30,
        requested_end=datetime(2024, 1, 30, 23, 0, tzinfo=timezone.utc),
    )
    rest_kpis = await _run_portfolio_backtest(rest_frame)
    assert rest_kpis["data_window"][0]["source"] == "rest_api"
    assert rest_kpis["data_window"][0]["differs_from_rest"] is False


def test_the_disclosure_survives_the_api_the_customer_reads():
    """The API serves the KPI dict as it is, so the disclosure is served.

    The disclosure lives INSIDE ``kpi_results_json``, which is a free-form
    dictionary: no public shape changes and nothing is dropped on the way
    to the customer.
    """
    kpis = {
        "total_pnl": 12.5,
        "sharpe_ratio": 1.2,
        "data_window": [
            {
                "data_key": "kline_1h",
                "source": "bulk_archive",
                "complete": False,
                "missing_buckets": [
                    {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"}
                ],
                "reason": "the archive is missing a day",
            }
        ],
    }

    details = schemas.BacktestRunDetails(
        id="run-1",
        task_id="task-1",
        strategy_name="VolumeBreakout",
        symbol="BTCUSDT",
        status="COMPLETED",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2024, 1, 31, tzinfo=timezone.utc),
        initial_balance=10000.0,
        parameters_json={},
        kpi_results_json=kpis,
        trades=[],
    )

    served = details.model_dump()["kpi_results_json"]
    assert "data_window" in served, (
        "the disclosure has to reach the customer: a result that is served "
        "without it is a result that says nothing about its window"
    )
    assert served["data_window"][0]["source"] == "bulk_archive"
    assert served["data_window"][0]["complete"] is False
    assert served["data_window"][0]["missing_buckets"]


# ==============================================================================
# E4: a REST-sourced backtest keeps the numbers it always had
# ==============================================================================


async def test_a_rest_sourced_backtest_keeps_its_numbers():
    """The disclosure ADDS to the result; it does not change it.

    The numbers a customer reads must be the numbers the backtest has
    always produced for the same candles, and a REST-sourced backtest that
    covers its window must not be flagged.
    """
    frame = _frame_with_provenance(
        source="rest_api",
        bars=24 * 30,
        requested_end=datetime(2024, 1, 30, 23, 0, tzinfo=timezone.utc),
    )
    with_provenance = await _run_portfolio_backtest(frame)

    bare = frame.copy()
    bare.attrs = {}
    without_provenance = await _run_portfolio_backtest(bare)

    def _numbers(kpis):
        return {k: v for k, v in kpis.items() if k != "data_window"}

    assert _numbers(with_provenance) == _numbers(without_provenance), (
        "the disclosure must not change the numbers a REST-sourced backtest "
        "produces"
    )
    assert with_provenance["data_window"][0]["complete"] is True
    assert with_provenance["data_window"][0]["source"] == "rest_api"


# ==============================================================================
# E6: the portfolio result carries the disclosure to the customer
# ==============================================================================


async def test_the_portfolio_kpis_carry_the_disclosure():
    """The portfolio KPIs are a CLOSED model, and a closed model drops what
    it does not declare.

    The disclosure is computed by the backtester -- it is in the dict the
    run saves -- and was then thrown away at the schema boundary, so a
    portfolio backtest over a shorter window reported the numbers of the
    shorter window as the numbers of the window that was asked for.
    """
    frame = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )

    results = await _run_portfolio_backtest(frame)

    # THE BACKTESTER COMPUTES IT ...
    assert results["data_window"], "the backtester must compute the disclosure"
    assert results["data_window"][0]["complete"] is False

    # ... AND THE SCHEMA THAT HOLDS THE PORTFOLIO KPIs MUST CARRY IT.
    kpis = schemas.PortfolioBacktestKPIs(**results).model_dump()
    assert "data_window" in kpis, (
        "the portfolio KPIs drop what they do not declare: the disclosure is "
        "computed and then thrown away, which is the same as no disclosure"
    )
    assert kpis["data_window"][0]["complete"] is False
    assert kpis["data_window"][0]["missing_buckets"]
    assert kpis["data_window"][0]["source"] == "bulk_archive"


async def test_the_portfolio_result_carries_the_disclosure_to_the_response():
    """The disclosure has to reach the RESPONSE, not just the model.

    The response is what the customer reads.  The disclosure rides in the
    same place the single-strategy result already carries it: inside the
    KPI payload, under the key the two paths have in common.
    """
    frame = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )

    results = await _run_portfolio_backtest(frame)

    response = schemas.PortfolioBacktestResult(
        portfolio_kpis=results,
        sliced_analytics={
            "by_strategy": {},
            "by_symbol": {},
            "by_strategy_symbol": {},
        },
        trade_log=[],
        equity_curve=[],
    )
    stored = response.model_dump()

    assert "data_window" in stored["portfolio_kpis"], (
        "the disclosure has to be in the response the customer reads"
    )
    assert stored["portfolio_kpis"]["data_window"][0]["complete"] is False
    assert stored["portfolio_kpis"]["data_window"][0]["missing_buckets"]


async def test_a_portfolio_result_without_a_disclosure_is_still_valid():
    """The field is OPTIONAL: a result that carries no disclosure still
    validates, and its numbers are unchanged."""
    frame = _frame_with_provenance(
        source="rest_api",
        bars=24 * 30,
        requested_end=datetime(2024, 1, 30, 23, 0, tzinfo=timezone.utc),
    )

    results = await _run_portfolio_backtest(frame)
    results.pop("data_window", None)

    kpis = schemas.PortfolioBacktestKPIs(**results).model_dump()

    assert kpis["data_window"] is None
    assert kpis["total_trades"] == results["total_trades"]
    assert kpis["final_balance"] == results["final_balance"]


def test_the_single_strategy_api_shape_is_unchanged():
    """The single-strategy result serves the KPI payload as a free-form
    dictionary.  It is NOT touched by the portfolio change: a payload
    without a disclosure is served exactly as it was, with no new key."""
    details = schemas.BacktestRunDetails(
        id="run-1",
        task_id="task-1",
        strategy_name="VolumeBreakout",
        symbol="BTCUSDT",
        status="COMPLETED",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2024, 1, 31, tzinfo=timezone.utc),
        initial_balance=10000.0,
        parameters_json={},
        kpi_results_json={"total_pnl": 12.5, "sharpe_ratio": 1.2},
        trades=[],
    )

    served = details.model_dump()["kpi_results_json"]

    assert served == {"total_pnl": 12.5, "sharpe_ratio": 1.2}, (
        "the single-strategy API shape must not change: a client that reads "
        "this payload today must see exactly what it saw before"
    )


# ==============================================================================
# E5: the disclosure is attached where the numbers are saved
# ==============================================================================


def test_the_disclosure_is_attached_to_the_kpis_the_run_saves():
    """The KPI dict a run saves carries the whole truth."""
    frame = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )
    kpis = {"total_pnl": 12.5}

    # The producers attach the loader's notes FIRST, and the disclosure
    # carries them: the numbers the customer is told are the numbers the
    # loader measured.
    data_loader.attach_klines_window_coverage(kpis, {"kline_1h": frame})
    records = attach_candles_disclosure(kpis, {"kline_1h": frame})

    assert records
    assert kpis["data_window"][0]["data_key"] == "kline_1h"
    assert kpis["data_window"][0]["complete"] is False
    assert kpis["data_window"][0]["missing_buckets"]
    assert kpis["data_window"][0]["trailing_edge_short_by_seconds"] == 2 * 24 * 3600
    # The numbers the run already had are untouched.
    assert kpis["total_pnl"] == 12.5


def test_a_frame_the_loader_calls_short_is_never_reported_as_complete():
    """A frame that is short stays short, even when its note is missing.

    The note carries the NUMBERS of the gap; the verdict is the loader's.
    A frame the loader says is short must never be reported as COMPLETE,
    because "nobody looked" is not "nothing was missing".
    """
    frame = _frame_with_provenance(
        missing_buckets=[
            {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        ],
        trailing_short=2 * 24 * 3600,
    )
    kpis = {"total_pnl": 12.5}

    # The loader's notes were never attached.
    records = attach_candles_disclosure(kpis, {"kline_1h": frame})

    assert records
    assert records[0]["complete"] is False


def test_the_disclosure_does_not_invent_a_frame():
    """No frames, no disclosure -- and the KPI dict is left alone."""
    kpis = {"total_pnl": 12.5}

    assert attach_candles_disclosure(kpis, {}) == []
    assert attach_candles_disclosure(kpis, None) == []
    assert "data_window" not in kpis
