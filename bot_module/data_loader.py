# bot_module/data_loader.py
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

import backoff
import pandas as pd
import requests
from requests.adapters import HTTPAdapter, Retry

from . import config
from .bulk_archive import (
    COVERAGE_GAP_ATTR,
    DIFFERENCE_ATTR,
    BulkArchiveCoverageError,
    fetch_klines_from_bulk_archive,
    interval_duration,
)
from .utils import (
    add_relative_volume,
    calculate_scalper_natr,
    add_volume_percentile_rank,
)

import asyncio
import functools

logger = logging.getLogger("bot_module.data_loader")

# Configuring HTTP session for fault tolerance
retry_strategy = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[
        418,
        429,
        500,
        502,
        503,
        504,
    ],  # Standard server error codes + rate limit
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http_session = requests.Session()
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

# Constants for DataFrame columns
KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
]
AGGTRADE_COLUMNS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "timestamp",
    "is_buyer_maker",
]

# The key under which the loader records WHICH source produced a frame
# ("rest_api", "bulk_archive" or "local_cache").
SOURCE_ATTR = "klines_source"

# The key under which the loader records the PROVENANCE of a frame: where
# it came from, which window was asked for, which window was delivered and
# whether the frame differs from what the REST path would have returned.
#
# Every frame the loader returns carries one, so a caller that asks for a
# window can always tell what it got -- and a frame that came from the
# bulk archive also carries the DIFFERENCE record (``DIFFERENCE_ATTR``),
# which says exactly HOW it differs.
PROVENANCE_ATTR = "klines_provenance"

# The key under which the loader records the COVERAGE NOTE of a frame:
# what ``klines_window_coverage`` says about the window the frame
# ACTUALLY covers, as opposed to the window that was asked for.
#
# THE NOTE TRAVELS WITH THE FRAME, so every consumer of a frame the
# loader returns can ask "what did I actually get?" without knowing
# which call site was instrumented.  The note used to be computed by the
# loader and read by TWO hand-picked call sites, so the trainer, the
# data consumer, the diagnostics routes, the API and two of the celery
# paths could all receive a shortened, hole-riddled archive frame with
# nothing at all saying so.
COVERAGE_ATTR = "klines_window_coverage"


def klines_provenance(frame: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """The provenance of ``frame``: where it came from and what it covers.

    ``None`` for a frame that carries no provenance.  The dict holds
    ``source`` ("rest_api", "bulk_archive" or "local_cache"),
    ``requested_start``, ``requested_end``, ``actual_start``,
    ``actual_end``, ``rows`` and ``differs_from_rest``, and the
    ``interval`` the frame's bars are on (which says how long the last
    bar covers, and so where the frame's coverage ends).  A frame that
    came from the bulk archive also holds its ``difference`` record, which
    names every way in which it differs from the frame the REST path would
    have returned.
    """
    if frame is None:
        return None
    provenance = frame.attrs.get(PROVENANCE_ATTR)
    if isinstance(provenance, dict):
        return dict(provenance)
    return None


def klines_window_coverage(frame: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """The window ``frame`` ACTUALLY covers, when it is not the one asked for.

    The loader already records WHAT it delivered (``klines_provenance``) and
    HOW it differs from the frame the REST path would have returned (the
    difference record).  Those two records used to be read by NOBODY outside
    this module, so a backtest could report the KPIs of a SHORTER window as
    if they were the KPIs of the window that was asked for.

    This helper is the ONE place that turns those records into the sentence a
    caller can act on, so every caller asks the same question the same way
    (see AGENTS.md: a predicate that is duplicated must handle the same
    special cases in every copy).

    ``None`` when the frame covers the whole window that was requested -- the
    ordinary case, and the case where there is nothing to say.  Otherwise a
    dict holding ``source``, ``requested_start``, ``requested_end``,
    ``actual_start``, ``actual_end``, ``rows``, ``differs_from_rest``,
    ``trailing_edge_short_by_seconds``, ``leading_shortfall``,
    ``remote_failed`` and a ``reason`` written in words.
    """
    if frame is None:
        return None
    provenance = klines_provenance(frame)
    if provenance is None:
        return None
    difference = provenance.get("difference")
    if not isinstance(difference, dict):
        difference = {}
    trailing_short = float(difference.get("trailing_edge_short_by_seconds") or 0.0)
    leading_shortfall = difference.get("leading_shortfall")
    remote_failed = bool(provenance.get("remote_failed"))
    # THE INTERIOR HOLES TRAVEL WITH THE NOTE.  The archive names the days
    # it does not hold on the frame itself ("missing_buckets") and SERVES
    # the frame with them, because the data that exists is worth more than
    # no data.  A note that said nothing about them would leave the caller
    # -- and the KPI payload the caller saves -- with no sign of a hole the
    # data really has.  The note used to return None for a frame whose ONLY
    # gap was an interior one, so kpis["data_window"] was ABSENT for
    # exactly the backtests that need it.
    missing_buckets = difference.get("missing_buckets")
    if not isinstance(missing_buckets, list):
        missing_buckets = []
    # THE LEADING EDGE IS MEASURED HERE, NOT TAKEN FROM THE RECORD.  The
    # archive only records a leading shortfall when it can PROVE the symbol
    # was listed later than the window asks for; a leading month whose
    # monthly archive is PUBLISHED but TRUNCATED leaves no record at all,
    # and the frame then starts later than the window without anybody
    # saying so.  The delivered window and the requested one are compared
    # directly, so that case is reported too.
    leading_gap = _seconds_between(
        provenance.get("actual_start"), provenance.get("requested_start")
    )
    # THE TRAILING EDGE IS MEASURED HERE WHEN THE RECORD DOES NOT CARRY IT.
    # The archive's own record measures ITS trailing edge; a REST or a
    # cached frame has no difference record at all, so nothing measured
    # its trailing edge: a frame that ends BEFORE the window that was asked
    # for (a symbol whose data stops there) got no note at all, and the
    # KPIs of that shorter window were reported as the KPIs of the window
    # that was asked for -- the one thing this note exists to stop.  The
    # frame carries the bars' OPEN times, so the window it COVERS ends
    # with the last bar's CLOSE, and the interval says how long that bar
    # covers: the same arithmetic the archive's own record uses.
    if trailing_short <= 0:
        trailing_short = _delivered_trailing_short(provenance)
    if (
        trailing_short <= 0
        and leading_gap <= 0
        and not remote_failed
        and not missing_buckets
    ):
        return None
    # THE REASON NAMES THE HOLDER OF THE MISSING DAYS, AND THE HOLDER
    # DEPENDS ON THE SOURCE.  The archive is NOT the holder of every gap:
    # a REST frame that starts late is Binance's OWN answer (a symbol
    # listed after the window that was asked for, for instance), and a
    # cached frame's gap belongs to the cache.  Blaming the archive for a
    # gap the archive never saw was a false statement in the note the
    # caller carries -- and this note is what the user is shown.
    if provenance.get("source") == "bulk_archive":
        holder = "the archive"
    elif provenance.get("source") == "rest_api":
        holder = "the Binance REST API"
    elif provenance.get("source") == "local_cache":
        holder = "the local cache"
    else:
        holder = "the source"
    if remote_failed:
        reason = (
            "the remote source failed and this frame is the cached history "
            "only, so it does not cover the whole window"
        )
    elif leading_gap > 0:
        reason = (
            f"the frame starts {leading_gap:.0f}s after the requested start; "
            f"{holder} has no candles for the days in between"
        )
    elif trailing_short > 0:
        reason = (
            f"the frame ends {trailing_short:.0f}s before the requested end; "
            f"{holder} has no candles for the days in between"
        )
    else:
        reason = (
            f"the frame covers the window it was asked for, but {holder} "
            "does not hold every day in between"
        )
    if missing_buckets:
        # The holes are named on the frame, and they are named HERE too:
        # the note is what reaches the KPI payload.
        reason = (
            f"{reason}; {len(missing_buckets)} day(s) inside the window the "
            "frame covers are missing from it"
        )
    return {
        "source": provenance.get("source"),
        "requested_start": provenance.get("requested_start"),
        "requested_end": provenance.get("requested_end"),
        "actual_start": provenance.get("actual_start"),
        "actual_end": provenance.get("actual_end"),
        "rows": provenance.get("rows"),
        "differs_from_rest": bool(provenance.get("differs_from_rest")),
        "trailing_edge_short_by_seconds": trailing_short,
        "leading_gap_seconds": leading_gap,
        "leading_shortfall": leading_shortfall,
        "missing_buckets": missing_buckets,
        "remote_failed": remote_failed,
        "reason": reason,
    }


# THE MARKER every caller logs when the frame it holds does not cover
# the window it asked for.  It is greppable on purpose: a shortened
# training window must be FINDABLE in a log, not swallowed by a generic
# message.
COVERAGE_LOG_MARKER = "klines_data_window_not_as_requested"


def klines_coverage_note(frame: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """The coverage note the loader attached to ``frame``, if it has one.

    ``None`` for a frame that carries no note -- which is the case for a
    frame that covers the window that was asked for: there is nothing to
    say about that one.

    The note is read FROM THE FRAME.  A caller that cannot find it there
    has been handed a frame nobody disclosed, and that has to be visible
    rather than papered over by recomputing it here.
    """
    if frame is None:
        return None
    attrs = getattr(frame, "attrs", None)
    if not isinstance(attrs, dict):
        return None
    note = attrs.get(COVERAGE_ATTR)
    if isinstance(note, dict):
        return dict(note)
    return None


def _attach_klines_coverage_note(
    frame: Optional[pd.DataFrame],
) -> Optional[pd.DataFrame]:
    """Puts the coverage note ON the frame, where the frame is built.

    THIS IS THE FRAME BOUNDARY.  Every consumer of the loader's frames
    gets the disclosure from here -- not from the two call sites that
    were instrumented by hand, which is how a trainer could train on a
    shortened, hole-riddled archive frame with nothing saying so.
    """
    if frame is None:
        return None
    note = klines_window_coverage(frame)
    if note:
        frame.attrs[COVERAGE_ATTR] = note
    return frame


def log_klines_window_coverage(
    frame: Optional[pd.DataFrame], log_prefix: str = ""
) -> Optional[Dict[str, Any]]:
    """Logs the coverage note of ``frame``, when it has one.

    ONE place formats it, so every caller says the same thing about the
    same frame (see AGENTS.md: a predicate that is duplicated must
    handle the same special cases in every copy).

    The note is READ FROM THE FRAME, not recomputed: the disclosure
    travels with the data, and a caller that cannot find it on the frame
    it holds has been given a frame nobody disclosed.
    """
    note = klines_coverage_note(frame)
    if not note:
        return None
    logger.warning(
        f"{log_prefix}{COVERAGE_LOG_MARKER} source={note.get('source')} "
        f"requested={note.get('requested_start')}..{note.get('requested_end')} "
        f"delivered={note.get('actual_start')}..{note.get('actual_end')} "
        f"rows={note.get('rows')}: {note.get('reason')}"
    )
    return note


def _delivered_trailing_short(provenance: Dict[str, Any]) -> float:
    """The seconds between the requested end and the DELIVERED frame's end.

    The frame carries the bars' OPEN times, so the window it covers ends
    with the last bar's CLOSE, and the interval says how long one bar
    covers.  ``0.0`` when it cannot be said: a record without an interval,
    or without both ends, says nothing, and a coverage note that guesses
    would be worse than no note at all.
    """
    interval = provenance.get("interval")
    actual_end = provenance.get("actual_end")
    requested_end = provenance.get("requested_end")
    if not interval or not actual_end or not requested_end:
        return 0.0
    try:
        covered_end = datetime.fromisoformat(actual_end) + interval_duration(interval)
        wanted_end = datetime.fromisoformat(requested_end)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (wanted_end - covered_end).total_seconds())


def _seconds_between(later: Optional[str], earlier: Optional[str]) -> float:
    """The seconds between two ISO timestamps, or 0.0 when it cannot be said.

    ``later`` and ``earlier`` are the ISO strings the provenance record
    holds.  A record that does not carry both of them says nothing, and a
    coverage note that guesses would be worse than no note at all.
    """
    if not later or not earlier:
        return 0.0
    try:
        later_dt = datetime.fromisoformat(later)
        earlier_dt = datetime.fromisoformat(earlier)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (later_dt - earlier_dt).total_seconds())


def klines_window_coverages(frames: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The coverage of every loaded frame that does not cover its window.

    ``frames`` maps a data key to the frame that was loaded ("kline_1h" ->
    the hourly frame, and so on).  The result is a LIST, because every frame
    has its own window and its own coverage, and a caller that has to be
    told about the window its numbers were computed over needs all of them.

    This is the ONE place that turns the records the loader keeps into the
    note a caller carries, so every caller asks the same question the same
    way (see AGENTS.md: a predicate that is duplicated must handle the same
    special cases in every copy).
    """
    notes: List[Dict[str, Any]] = []
    if not frames:
        return notes
    for key, frame in frames.items():
        coverage = klines_window_coverage(frame)
        if coverage:
            coverage = dict(coverage)
            coverage["data_key"] = str(key)
            notes.append(coverage)
    return notes


def attach_klines_window_coverage(
    kpis: Optional[Dict[str, Any]],
    frames: Optional[Dict[str, Any]],
    log_prefix: str = "",
) -> Optional[List[Dict[str, Any]]]:
    """Puts the coverage of the loaded frames ON the KPI dict a run saves.

    The loader keeps the records; this is what puts them where the user can
    see them.  A record that nobody reads is a record that does not happen,
    and the KPIs of a shorter window would be reported as the KPIs of the
    window that was asked for.

    ONE place does it, so every caller carries the coverage the same way
    (see AGENTS.md: a predicate that is duplicated must handle the same
    special cases in every copy).
    """
    notes = klines_window_coverages(frames)
    if not notes:
        return None
    if isinstance(kpis, dict):
        kpis["data_window"] = list(notes)
    for note in notes:
        logger.warning(
            f"{log_prefix}The data covers a window that is NOT the one that was "
            f"requested: {note.get('reason')} (requested "
            f"{note.get('requested_start')} .. {note.get('requested_end')}, "
            f"delivered {note.get('actual_start')} .. {note.get('actual_end')})."
        )
    return list(notes)


def _provenance_record(
    source: str,
    requested_start: datetime,
    requested_end: datetime,
    frame: pd.DataFrame,
    differs_from_rest: bool = False,
    difference: Optional[Dict[str, Any]] = None,
    remote_failed: bool = False,
    note: Optional[str] = None,
    interval: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds the provenance record the loader attaches to every frame."""
    record = {
        "source": source,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": frame.index[0].isoformat() if not frame.empty else None,
        "actual_end": frame.index[-1].isoformat() if not frame.empty else None,
        "rows": int(len(frame)),
        "differs_from_rest": differs_from_rest,
    }
    # THE INTERVAL TRAVELS WITH THE RECORD.  The frame carries the bars'
    # OPEN times, so the window it COVERS ends with the last bar's CLOSE,
    # and only the interval says how long that bar covers.  Without it the
    # trailing edge of a frame that is not from the archive cannot be
    # measured at all.
    if interval:
        record["interval"] = interval
    if difference is not None:
        record["difference"] = difference
    if remote_failed:
        record["remote_failed"] = True
    if note:
        record["note"] = note
    return record


def _difference_is_truthful(frame: pd.DataFrame, difference: Any) -> bool:
    """True when the difference record matches the frame it describes.

    The record is the only thing that tells a caller that the frame it
    holds is not the frame the REST path would have returned.  A record
    that does not describe the frame it travels with is worse than no
    record at all: it is a false statement about the data.
    """
    if not isinstance(difference, dict):
        return False
    if difference.get("source") != "bulk_archive":
        return False
    if frame.empty:
        return False
    if difference.get("actual_start") != frame.index[0].isoformat():
        return False
    if difference.get("actual_end") != frame.index[-1].isoformat():
        return False
    return True


def _process_indicators_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Adds the indicator columns the strategies need to a klines frame."""
    df_processed = df.copy()
    df_processed = add_relative_volume(df_processed, period=200)
    df_processed = calculate_scalper_natr(df_processed, period=30)
    df_processed = add_volume_percentile_rank(
        df_processed, period=1000, percentile=90
    )
    return df_processed


# ==============================================================================
# Helper functions
# ==============================================================================


def get_local_path(
    symbol: str, data_type: str, market_type: str, timeframe: Optional[str] = None
) -> Path:
    """Determines the path to the local Parquet file for a given data type."""
    # THE VENUE IS DECIDED IN ONE PLACE.  ``config.resolve_market_venue`` is
    # the SAME function the REST path and the bulk archive ask, so the cache
    # can never be filed under a folder that belongs to another venue.
    #
    # It used to be ``"futures" if "futures" in market_type else "spot"``,
    # which was a SECOND, hand-written copy of the venue predicate and it
    # disagreed with the real one: the "coinm" alias went to the SPOT folder
    # while the REST path and the archive answered it with COIN-M candles,
    # and "spot_usdtm" went to the SPOT folder while the REST path queried
    # the USDT-M host.  A venue predicate that is duplicated must handle the
    # same special cases in every copy (see AGENTS.md).
    #
    # CORRECTION (2026-11-02): "the cache can never be filed under a
    # folder that belongs to another venue" is true at the granularity of
    # the FOLDER, which separates SPOT from FUTURES, and it was wrong at
    # the granularity of the VENUE.  The two futures venues share the
    # "futures" folder, because their SYMBOLS differ: USDT-M is BTCUSDT
    # and COIN-M is BTCUSD_PERP.  So a COIN-M request for a USDT-quoted
    # symbol still finds the USDT-M cache entry and is answered from it,
    # with no API call at all.  That is PRE-EXISTING behaviour, unchanged
    # by this branch, and it needs a COIN-M request with a USDT-quoted
    # symbol to reach it -- a request that is itself a mistake, and that
    # the API path refuses loudly (dapi answers 400 for BTCUSDT, which is
    # not a 451, so it is re-raised).  The two paths therefore disagree
    # about that one mistake: the cold path fails loud, the warm path
    # answers from the cache.
    venue = config.resolve_market_venue(market_type)
    market_folder = "spot" if venue == "spot" else "futures"

    filename = ""
    if data_type == "klines":
        if not timeframe:
            raise ValueError("Timeframe must be provided for klines data type.")
        filename = f"kline_{timeframe}.parquet"
    elif data_type == "aggTrades":
        filename = "aggTrade.parquet"  # The filename for partitions will be different, this is for the old format
    elif data_type == "open_interest":
        filename = "open_interest.parquet"
    elif data_type == "aggregated_depth":
        filename = "aggregated_depth.parquet"
    else:
        raise ValueError(f"Unknown data type {data_type}")

    return (
        Path(config.LOCAL_DATA_STORAGE_PATH)
        / "binance"
        / market_folder
        / symbol.upper()
        / filename
    )


# The phrases Binance's OWN geo-block answer carries.  MEASURED on
# 2026-11-02 against the live blocked endpoint:
#
#   {"code": 0, "msg": "Service unavailable from a restricted location
#    according to 'b. Eligibility' in https://www.binance.com/en/terms.
#    Please contact customer service if you believe you received this
#    message in error."}
#
# HTTP 451 is ALSO emitted by corporate proxies and web application
# firewalls (RFC 7725), and a proxy's 451 body says NOTHING about
# Binance.  A proxy-451 answered with archive data would swap the source
# of the candles without anybody knowing, so the fallback only runs when
# the body says the block is Binance's.
# THE PHRASE THAT IDENTIFIES BINANCE.
#
# MEASURED 2026-11-02 against the live blocked endpoints (api, fapi and
# dapi all answer 451 with the SAME body):
#
#   {"code": 0, "msg": "Service unavailable from a restricted location
#    according to 'b. Eligibility' in https://www.binance.com/en/terms.
#    Please contact customer service if you believe you received this
#    message in error."}
#
# "restricted location" is the GENERIC half of that sentence: it says the
# request was blocked because of where it came from, and a corporate proxy
# or a WAF can write exactly that.  "binance" is no better: a proxy's
# error page quotes the URL it blocked, and that URL says binance.
# "b. eligibility" is the half that says WHO refused -- it is Binance
# citing its own Terms of Use, and no proxy writes it.
#
# The check used to accept EITHER half, so a proxy whose body happened to
# say "restricted location" was answered from the archive: the candles
# came from a different source, and the fallback had not been armed by
# Binance at all.  Only Binance's own phrase arms it now.  A body that
# stops carrying it fails the check, and that fails LOUD (the REST error
# is re-raised unchanged, and the log says why) instead of quietly
# swapping the source of the candles.
_BINANCE_GEO_BLOCK_MARKER = "b. eligibility"


def _is_binance_geo_block_body(response: Any) -> bool:
    """True when a 451 body is Binance's own geo-block answer.

    The status code alone does not say WHO refused: RFC 7725 makes 451
    "Unavailable For Legal Reasons" a status a corporate proxy or a WAF
    answers with.  Only the body tells the two apart, so the body is
    what the fallback checks.

    AND THE BODY HAS TO NAME BINANCE.  The generic phrase on its own does
    not: "access denied from a restricted location" is a sentence every
    proxy can write, and a proxy's block must not be answered with archive
    data.  So the body must carry Binance's own phrase.
    """
    try:
        text = response.text
    except Exception:  # pragma: no cover - a body that cannot be read
        return False
    if not text:
        return False
    return _BINANCE_GEO_BLOCK_MARKER in text.lower()


def _is_binance_location_block(error: Exception) -> bool:
    """True when the REST endpoint refused us because of where we are.

    Binance answers HTTP 451 ("Service unavailable from a restricted
    location according to 'b. Eligibility' ...") from geo/IP-blocked
    networks.  That is the signature this repository already records for the
    production servers: ``market_data_service.py:155-157`` ("Binance is
    geo-blocked from many Elestio server IPs (HTTP 451)") and
    ``bot_runner.py:612`` ("a fresh Binance consumer that 451s on REST
    history").

    ONLY HTTP 451 counts.  Every other failure -- a 4xx/5xx answer, a bad
    request, a bug, a DNS/TLS failure, a timeout, an exhausted retry -- keeps
    the behaviour it has today and is re-raised unchanged.  A genuine fault
    must never be masked by silently serving archive data instead: the
    archive holds only closed bars and its trailing edge is short by the
    publication lag, so serving it in place of a fault could quietly shorten
    a backtest window and change the number it reports.

    AND THE 451 MUST BE BINANCE'S.  A 451 from a corporate proxy or a WAF
    is answered with the proxy's own body, which says nothing about
    Binance.  The fallback is only allowed to answer a request Binance
    itself refused, so the body is checked as well as the status code: a
    451 whose body is not Binance's geo-block answer is NOT a location
    block, and the error is re-raised unchanged.
    """
    if isinstance(error, requests.exceptions.HTTPError):
        response = getattr(error, "response", None)
        if response is None or response.status_code != 451:
            return False
        if _is_binance_geo_block_body(response):
            return True
        logger.error(
            "A 451 came from something other than Binance: the response body "
            "is not Binance's geo-block answer. The bulk-archive fallback is "
            "NOT used for it, because a proxy's block must not be answered "
            "with archive data."
        )
        return False
    return False


# THE ENDPOINTS THAT HAVE A BULK-ARCHIVE FALLBACK.  A Binance geo-block
# (HTTP 451) on one of these is ANSWERED by the archive, so the extra
# attempts could not succeed and only delayed the fallback that does
# work.  EVERY OTHER ENDPOINT HAS NO FALLBACK TO GET TO, so its five
# tries are the only chance to get through a block that flaps, and they
# are kept.
_ENDPOINTS_WITH_ARCHIVE_FALLBACK = frozenset({"klines"})


def _perform_api_request(
    endpoint: str, params: Optional[Dict[str, Any]] = None, market_type: str = "spot"
) -> List[Any]:
    """Performs ONE request to the Binance API with consideration of the environment (mainnet/testnet).

    NO RETRY LOGIC IS HERE.  What a failure MEANS depends on whether the
    caller has a fallback to go to, and only the caller knows that, so
    the decision is made by the two wrappers below.

    THE VENUE IS DECIDED IN ONE PLACE.  ``config.resolve_market_venue``
    maps the market type onto the venue, and the bulk-archive fallback asks
    the same function, so the two can never answer for different markets.
    A COIN-M request therefore goes to the COIN-M host and to the COIN-M
    archive folder, instead of both of them being answered with USDT-M
    candles.
    """
    venue = config.resolve_market_venue(market_type)
    if config.ACTIVE_TRADING_ENVIRONMENT == "testnet":
        # Binance has no COIN-M testnet of its own, so the futures testnet
        # host answers every futures venue.
        base_url = (
            config.BINANCE_SPOT_TESTNET_API_URL_FOR_LOADER
            if venue == "spot"
            else config.BINANCE_FUTURES_TESTNET_API_URL_FOR_LOADER
        )
    else:  # mainnet
        base_url = {
            "spot": config.BINANCE_SPOT_DATA_API_URL_FOR_LOADER,
            "futures_usdtm": config.BINANCE_FUTURES_USDTM_DATA_API_URL_FOR_LOADER,
            "futures_coinm": config.BINANCE_FUTURES_COINM_DATA_API_URL_FOR_LOADER,
        }[venue]

    url = f"{base_url}/{endpoint}"

    logger.debug(f"DataLoader: Making API request to: {url} with params: {params}")
    response = http_session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=5,
    max_time=60,
    logger=logger,
    giveup=_is_binance_location_block,
)
def _make_api_request_giveup_on_geo_block(
    endpoint: str, params: Optional[Dict[str, Any]] = None, market_type: str = "spot"
) -> List[Any]:
    """A request whose caller HAS an archive fallback to go to.

    A GEO-BLOCK IS NOT RETRIED HERE.  ``giveup=_is_binance_location_block``
    stops the retry loop the moment the answer is HTTP 451, and nothing else:
    every other failure keeps the five tries and the 60 second budget it
    has always had.

    The reason is measured, not assumed: before this change a blocked load
    made FIVE requests to the same blocked host (the retry loop measured
    5.75s on this machine, with a jittered worst case of 15s and a 60s
    ceiling) before the bulk-archive fallback was even tried.  HTTP 451 is
    Binance's deterministic answer to a request from a blocked address --
    the same request from the same address gets the same answer a second
    later -- so those four extra attempts could not succeed and only
    delayed the fallback that does work.

    ONLY A CALLER WITH A FALLBACK MAY USE THIS.  On an endpoint with no
    fallback the four extra attempts were not delay at all: they were the
    only chance to get through a block that flaps.
    """
    return _perform_api_request(endpoint, params, market_type)


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=5,
    max_time=60,
    logger=logger,
)
def _make_api_request_with_retry_budget(
    endpoint: str, params: Optional[Dict[str, Any]] = None, market_type: str = "spot"
) -> List[Any]:
    """A request whose caller has NO fallback: EVERY failure keeps the five
    tries and the 60 second budget it has always had."""
    return _perform_api_request(endpoint, params, market_type)


def _make_api_request(
    endpoint: str, params: Optional[Dict[str, Any]] = None, market_type: str = "spot"
) -> List[Any]:
    """The ONE entry point every caller of the loader uses.

    THE GIVE-UP ON A GEO-BLOCK IS SCOPED TO THE CALLER THAT HAS A
    FALLBACK.  The give-up used to sit on this shared helper, so a 451
    aborted the aggTrades and the open-interest paths after ONE request
    -- two endpoints that have NO archive fallback to get to, and whose
    five tries were the only chance to get through a flapping block.  The
    klines path is the one WITH a fallback, so it is the one that gives
    up: one attempt, then the archive.

    A 451 THAT IS NOT BINANCE'S IS RE-RAISED UNCHANGED ON EVERY PATH:
    ``_is_binance_location_block`` / ``_is_binance_geo_block_body`` are
    the only things that decide it, and they decide it the same way here.
    """
    if endpoint in _ENDPOINTS_WITH_ARCHIVE_FALLBACK:
        return _make_api_request_giveup_on_geo_block(endpoint, params, market_type)
    return _make_api_request_with_retry_budget(endpoint, params, market_type)


# ==============================================================================
# Private functions for direct data downloading via API (blocking)
# ==============================================================================


def _download_klines_from_api(
    symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime, market_type: str
) -> Optional[pd.DataFrame]:
    """Downloads klines directly from Binance API."""
    logger.info(
        f"API CALL: Loading klines for {symbol} ({timeframe}) with {start_dt} for {end_dt}"
    )
    all_klines = []
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)

    current_start_ts = start_ts
    while current_start_ts < end_ts:
        params = {
            "symbol": symbol.upper(),
            "interval": timeframe,
            "startTime": current_start_ts,
            "limit": 1000,
            "endTime": end_ts,
        }
        klines_data = _make_api_request("klines", params, market_type=market_type)
        if not klines_data:
            break
        all_klines.extend(klines_data)
        current_start_ts = klines_data[-1][0] + 1

    if not all_klines:
        return pd.DataFrame()

    df = pd.DataFrame(all_klines, columns=KLINE_COLUMNS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time")
    numeric_cols = ["open", "high", "low", "close", "volume", "number_of_trades"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume", "number_of_trades"]]


def _download_klines_from_api_with_archive_fallback(
    symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime, market_type: str
) -> Optional[pd.DataFrame]:
    """Downloads klines via the Binance REST API, falling back to the bulk archive.

    The Binance REST market-data endpoints (api.binance.com / fapi.binance.com)
    answer HTTP 451 -- "Service unavailable from a restricted location" -- from
    geo/IP-blocked networks, which makes every backtest fail with "Failed to
    load historical data for <symbol>". Binance publishes the SAME candles as
    downloadable archives on data.binance.vision, so the loader can still
    produce data for the same venue, symbol and window.

    The REST path itself is untouched: it is still tried first and it is still
    what wins whenever it answers. The archive is a strictly additive third
    source, used ONLY when the REST path fails with a location block (HTTP
    451). Every other failure -- a 4xx/5xx, a bad request, a timeout, an
    exhausted retry, a bug -- is re-raised unchanged, so a genuine fault is
    never masked by quietly serving archive data instead.

    Two rules keep the venue honest and the failure honest:

    * The archive holds MAINNET Binance candles only, so it is never used
      when the loader is pointed at testnet.  A testnet request must never
      be answered with mainnet candles.
    * When the archive cannot supply the window, this returns ``None`` --
      exactly what the loader returned before the fallback existed -- so the
      existing hard stops still fire.  It never returns an empty frame in
      place of a failure: an empty frame is not the same thing as "no data
      loaded", and several callers treat them differently.

    THE TWO PATHS REPORT "NO DATA" DIFFERENTLY, AND BOTH ARE CORRECT.  The
    REST path is only reached when it ANSWERED, so "no klines in this
    window" is a successful load of an empty window and it stays an empty
    frame.  The archive path is only reached after the REST path was
    BLOCKED (HTTP 451), so an archive with nothing in it means the load
    produced no candles at all, and it is ``None`` -- the failure the
    callers' hard stops exist for.  Neither path ever serves a frame with
    a hole in it.

    CORRECTION (2026-11-02): the last sentence above is WRONG and is kept
    as it was written.  The ARCHIVE path DOES serve a frame with a hole in
    it: a day the archive does not hold, inside the range the symbol
    demonstrably traded through, is a hole, and that frame is SERVED with
    the gap RECORDED on it (the "MISSING N bucket(s) inside the traded
    range" path in ``bot_module/bulk_archive.py``).  The REST path has no
    hole to serve, because Binance's own klines answer for every bar it
    has.  What this paragraph is about is the "NO DATA" case, and THAT
    part was, and is, correct.
    """
    try:
        frame = _download_klines_from_api(
            symbol, timeframe, start_dt, end_dt, market_type
        )
        logger.info(
            f"klines_source=rest_api symbol={symbol} timeframe={timeframe} "
            f"rows={0 if frame is None else len(frame)}"
        )
        if frame is not None:
            frame.attrs[SOURCE_ATTR] = "rest_api"
        return frame
    except Exception as api_error:
        if not _is_binance_location_block(api_error):
            # Not a location block: keep the behaviour this loader has today.
            raise
        if config.ACTIVE_TRADING_ENVIRONMENT == "testnet":
            # data.binance.vision only publishes MAINNET candles. Serving them
            # for a testnet request would silently swap the venue.
            logger.error(
                f"Binance REST klines unavailable for {symbol} ({timeframe}) on "
                f"testnet: {api_error}. The bulk archive holds mainnet candles "
                "only and is not used on testnet."
            )
            raise
        logger.warning(
            f"Binance REST klines unavailable for {symbol} ({timeframe}): "
            f"{api_error}. Falling back to the Binance bulk archive."
        )

    try:
        frame = fetch_klines_from_bulk_archive(
            symbol, timeframe, start_dt, end_dt, market_type
        )
        if frame is None or frame.empty:
            # The archive produced no candles.  That is a FAILURE, and it
            # must look like one: an empty frame is not "no data loaded",
            # and trainer.py / data_consumer.py / portfolio_backtester.py
            # key on that difference to decide whether to hard-stop.
            logger.error(
                f"Bulk archive returned no klines for {symbol} ({timeframe}) "
                f"between {start_dt} and {end_dt}."
            )
            logger.info(
                f"klines_source=none symbol={symbol} timeframe={timeframe} rows=0"
            )
            return None
        logger.info(
            f"klines_source=bulk_archive symbol={symbol} timeframe={timeframe} "
            f"rows={len(frame)}"
        )
        # The SOURCE is stated on the frame, so the loader can tell an
        # archive frame from a REST frame and ENFORCE the difference record
        # that has to travel with it.
        frame.attrs[SOURCE_ATTR] = "bulk_archive"
        return frame
    except BulkArchiveCoverageError as coverage_error:
        logger.error(
            f"Bulk archive cannot cover the requested window for {symbol} "
            f"({timeframe}): {coverage_error}",
            exc_info=True,
        )
        logger.info(f"klines_source=none symbol={symbol} timeframe={timeframe} rows=0")
        return None
    except Exception as archive_error:
        logger.error(
            f"Bulk archive klines failed for {symbol} ({timeframe}): {archive_error}",
            exc_info=True,
        )
        logger.info(f"klines_source=none symbol={symbol} timeframe={timeframe} rows=0")
        return None


def _download_agg_trades_from_api(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str,
    limit_per_req: int = 1000,
    max_total_trades: int = 500000,
) -> Optional[pd.DataFrame]:
    """Downloads aggTrades directly with API Binance, using pagination for fromId."""
    logger.info(
        f"API CALL: Loading aggTrades for {symbol} with {start_dt} for {end_dt}"
    )
    all_trades = []
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    from_id = None

    while True:
        params = {"symbol": symbol.upper(), "limit": limit_per_req}
        if from_id is not None:
            params["fromId"] = from_id
        else:
            params["startTime"] = start_ts
            params["endTime"] = end_ts

        trades_data = _make_api_request("aggTrades", params, market_type=market_type)
        if not trades_data:
            break

        last_trade_in_batch = trades_data[-1]
        from_id = last_trade_in_batch["a"] + 1
        all_trades.extend(trades_data)

        if last_trade_in_batch["T"] >= end_ts or len(all_trades) >= max_total_trades:
            break

    if not all_trades:
        return pd.DataFrame()

    df = pd.DataFrame(all_trades)
    df.rename(
        columns={
            "a": "agg_trade_id",
            "p": "price",
            "q": "quantity",
            "T": "timestamp",
            "m": "is_buyer_maker",
        },
        inplace=True,
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df[(df.index >= start_dt) & (df.index <= end_dt)]
    df[["price", "quantity"]] = df[["price", "quantity"]].apply(
        pd.to_numeric, errors="coerce"
    )
    df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
    df = df.drop_duplicates(subset=["agg_trade_id"], keep="last")
    return df[["price", "quantity", "is_buyer_maker", "agg_trade_id"]]


def _download_open_interest_from_api(
    symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime, market_type: str
) -> Optional[pd.DataFrame]:
    """Downloads open interest history directly with API Binance."""
    if "futures" not in market_type:
        logger.warning(
            f"Open Interest is available only for futures. Skipping for {market_type}."
        )
        return None

    logger.info(
        f"API CALL: Loading Open Interest for {symbol} ({timeframe}) with {start_dt} for {end_dt}"
    )
    all_oi_data = []
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)

    current_start_ts = start_ts
    while current_start_ts < end_ts:
        params = {
            "symbol": symbol.upper(),
            "period": timeframe,
            "startTime": current_start_ts,
            "limit": 500,
            "endTime": end_ts,
        }
        try:
            oi_data = _make_api_request(
                "openInterestHist", params, market_type=market_type
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # Symbol does not support Open Interest History API (new coins, specific contracts)
                logger.warning(
                    f"Open Interest History is unavailable for {symbol} (404). Perhaps the symbol is new or does not support this function."
                )
                return pd.DataFrame()
            raise  # Reraising other HTTP errors

        if not oi_data:
            break
        all_oi_data.extend(oi_data)
        current_start_ts = oi_data[-1]["timestamp"] + 1

    if not all_oi_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_oi_data)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df["open_interest"] = pd.to_numeric(df["sumOpenInterest"], errors="coerce")
    df = df[~df.index.duplicated(keep="first")]
    return df[["open_interest"]]


# ==============================================================================
# "Smart" public functions (main module interface)
# ==============================================================================


async def download_klines(
    symbol: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str = "futures_usdtm",
) -> Optional[pd.DataFrame]:
    """
    (ASYNCHRONOUS) Hybrid klines loader.
    All blocking operations (file reading, HTTP requests) are executed in separate threads.

    THE FRAME ALWAYS SAYS WHERE IT CAME FROM.  Every frame this loader
    returns carries a PROVENANCE record in its ``attrs`` under the key
    ``PROVENANCE_ATTR`` ("klines_provenance"): the source ("rest_api",
    "bulk_archive" or "local_cache"), the window that was asked for, the
    window that was delivered, and whether the frame differs from what the
    REST path would have returned.  Read it with
    ``data_loader.klines_provenance(frame)``.

    A FRAME FROM THE BULK ARCHIVE IS NOT THE REST FRAME, AND THE LOADER
    ENFORCES THAT IT SAYS SO.  The archive holds only CLOSED bars, its
    trailing edge lags the REST path's by the publication lag, and it can
    be missing days the REST path has.  Those differences are MEASURED and
    recorded on the frame under ``DIFFERENCE_ATTR``
    ("bulk_archive_frame_difference"), and the loader checks that the
    record describes the frame it travels with: an archive-sourced frame
    whose record is missing or does not match it is REFUSED, because a
    frame nobody can tell apart from a REST frame is exactly the failure
    this fallback must not have.  Read the record with
    ``bulk_archive.frame_difference(frame)``.

    THE FRAME MAY COVER A SHORTER PERIOD THAN THE ONE REQUESTED.  When
    the REST API is geo-blocked (HTTP 451) the loader falls back to
    Binance's bulk archives, and the archives hold only what Binance
    published.  A symbol that was listed later than the requested window
    therefore comes back starting later.  That is NOT a failure when the
    archive can PROVE it (the symbol demonstrably did not trade then) --
    but when it cannot prove it, the load FAILS rather than serve a frame
    that is shorter than the window that was asked for.

    A caller that must not trade a shorter window than it asked for can
    see the shortfall on the returned frame: it is recorded in the frame's
    ``attrs`` under the key ``COVERAGE_GAP_ATTR``
    ("bulk_archive_coverage_gap"), as a dict with ``requested_start``,
    ``first_bar``, ``gap_days``, ``leading_month``,
    ``leading_month_published``, ``proven`` and ``reason``.  Read it with
    ``bulk_archive.coverage_gap(frame)``, which returns ``None`` when
    there is no shortfall.  The ``attrs`` do not change the frame's shape,
    its columns or its values, so the existing callers are unaffected.
    """
    try:
        if isinstance(start_dt, str):
            start_dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
        if isinstance(end_dt, str):
            end_dt = datetime.fromisoformat(end_dt.replace("Z", "+00:00"))

        loop = asyncio.get_running_loop()
        start_dt_utc = (
            start_dt.astimezone(timezone.utc)
            if start_dt.tzinfo
            else start_dt.replace(tzinfo=timezone.utc)
        )
        end_dt_utc = (
            end_dt.astimezone(timezone.utc)
            if end_dt.tzinfo
            else end_dt.replace(tzinfo=timezone.utc)
        )

        local_path = get_local_path(symbol, "klines", market_type, timeframe)
        df_local = None

        if local_path.exists():
            logger.info(f"Local klines file found: {local_path}. Reading...")
            df_local = await loop.run_in_executor(None, pd.read_parquet, local_path)
            df_local = df_local[
                (df_local.index >= start_dt_utc) & (df_local.index <= end_dt_utc)
            ]

        if df_local is not None and not df_local.empty:
            last_local_ts = df_local.index[-1]
            timeframe_delta = pd.to_timedelta(timeframe)
            is_complete = last_local_ts >= end_dt_utc - timeframe_delta
            has_indicators = (
                "natr" in df_local.columns and "is_volume_spike" in df_local.columns
            )

            if is_complete and has_indicators:
                logger.info(
                    "Local data is complete and contains indicators. Returning from cache."
                )
                logger.info(
                    f"klines_source=local_cache symbol={symbol} timeframe={timeframe} "
                    f"rows={len(df_local)}"
                )
                df_local.attrs[SOURCE_ATTR] = "local_cache"
                df_local.attrs[PROVENANCE_ATTR] = _provenance_record(
                    "local_cache", start_dt_utc, end_dt_utc, df_local, interval=timeframe
                )
                # THE NOTE IS ATTACHED HERE, ON THE FRAME, so every
                # consumer of a loader frame can read it.
                _attach_klines_coverage_note(df_local)
                return df_local

        api_start_dt = start_dt_utc
        if df_local is not None and not df_local.empty:
            api_start_dt = df_local.index[-1] + pd.to_timedelta(timeframe)

        df_api = await loop.run_in_executor(
            None,
            _download_klines_from_api_with_archive_fallback,
            symbol,
            timeframe,
            api_start_dt,
            end_dt_utc,
            market_type,
        )

        if df_api is None:
            # The remote source failed.  A usable LOCAL CACHE is still
            # served: it was served before the bulk-archive fallback
            # existed, and throwing it away turns a partial load into no
            # load at all -- a NEW hard failure where candles used to load.
            # (Before the fallback, this branch read ``if df_api is None and
            # (df_local is None or df_local.empty)``, and pandas' ``concat``
            # skips a None element, so the cache was merged and returned.)
            #
            # What must NOT happen is the caller believing it got the
            # window it asked for, so the cache-only frame carries a
            # provenance record that says the remote failed and that the
            # frame covers the CACHED period only.
            #
            # The REST path's own "no klines in this window" answer is a
            # DIFFERENT thing and is deliberately left alone: it is an EMPTY
            # frame (see ``_download_klines_from_api``), so the source
            # answered, and the merge below keeps the local cache, which is
            # the history that exists for the window.
            if df_local is not None and not df_local.empty:
                logger.warning(
                    f"Klines for {symbol} ({timeframe}): no data from the REST "
                    "API or the bulk archive. Serving the LOCAL CACHE only: the "
                    "frame does NOT cover the whole requested window."
                )
                df_cached = await loop.run_in_executor(
                    None, _process_indicators_frame, df_local
                )
                df_cached.attrs[SOURCE_ATTR] = "local_cache"
                df_cached.attrs[PROVENANCE_ATTR] = _provenance_record(
                    "local_cache",
                    start_dt_utc,
                    end_dt_utc,
                    df_cached,
                    remote_failed=True,
                    note=(
                        "the remote source failed; this frame is the cached "
                        "history only and does not cover the whole window"
                    ),
                    interval=timeframe,
                )
                # THE NOTE IS ATTACHED HERE, ON THE FRAME, so every
                # consumer of a loader frame can read it.
                _attach_klines_coverage_note(df_cached)
                return df_cached
            logger.error(
                f"Klines unavailable for {symbol} ({timeframe}): no data from "
                "the REST API or the bulk archive."
            )
            return None

        # THE SOURCE, THE DIFFERENCE AND THE LEADING-EDGE SHORTFALL the
        # remote frame carries.  They are captured HERE because the
        # operations below do not all carry them over: ``pandas.concat``
        # DROPS the ``attrs`` when the frames it merges do not all have the
        # same ones, and the local cache (read from parquet) has none.
        source = df_api.attrs.get(SOURCE_ATTR)
        difference = df_api.attrs.get(DIFFERENCE_ATTR)
        coverage_gap = df_api.attrs.get(COVERAGE_GAP_ATTR)
        if df_api.empty and df_local is not None and not df_local.empty:
            # The remote source ANSWERED, with nothing: the candles in the
            # frame that is about to be returned are the CACHE's, and the
            # provenance must say so.
            source = "local_cache"

        # THE DIFFERENCE IS ENFORCED, NOT TRUSTED.  A frame the archive
        # produced is NOT the frame the REST path would have returned, and
        # the record that says so has to travel with it.  An archive
        # frame whose record is missing, or that does not describe the
        # frame it travels with, is refused: a frame nobody can tell apart
        # from a REST frame is exactly the failure this fallback must not
        # have.
        #
        # CORRECTION (2026-11-02): the wording above is kept as it was
        # written, and "the frame it travels with" needs one sentence of
        # precision.  The record describes THE ARCHIVE'S CONTRIBUTION --
        # it is enforced against ``df_api``, the frame the archive
        # produced, which is exactly the frame it describes.  It then
        # travels on the MERGED frame, and the merged frame can be WIDER
        # than the archive's: the local cache covers the part of the
        # window the archive was not asked for (``api_start_dt`` starts
        # after the cache's last bar).  So the record's own
        # ``actual_start``/``actual_end`` are the ARCHIVE's, not the
        # delivered frame's.  The delivered frame's range is recorded
        # truthfully, by the provenance record below and by the coverage
        # note the caller carries; nothing reports the archive's range as
        # the delivered one.
        if source == "bulk_archive" and not _difference_is_truthful(df_api, difference):
            logger.error(
                f"Klines for {symbol} ({timeframe}): the bulk archive returned a "
                "frame whose difference record does not describe it. Refusing to "
                "serve a frame the caller cannot tell apart from a REST frame."
            )
            return None

        df_final = pd.concat([df_local, df_api]) if df_local is not None else df_api
        if df_final.empty:
            return df_final

        df_final = df_final[~df_final.index.duplicated(keep="last")].sort_index()

        # THE COVERAGE IS RECORDED AND DELIVERED, NOT ASSUMED.  A frame from
        # the bulk archive can be SHORTER than the window that was asked for
        # -- the archive holds closed bars only, it lags the REST path by the
        # publication lag, and it can be missing days the REST path has.
        # That frame is SERVED (the data that exists is worth more than no
        # data, and the REST path's own window is short in exactly the same
        # ways), but the difference record NAMES the gap, and the caller is
        # given that record so the KPIs of the run say which window they
        # were actually computed over.
        #
        # CORRECTION (2026-11-02): the loader REFUSED such a frame at first,
        # on the grounds that a shortfall nothing explains must not be
        # served.  The original wording of that rule is kept below, and it
        # was withdrawn: it contradicts the contract this loader already has
        # and already tests -- a genuinely missing daily file is SERVED and
        # NAMED, not fatal -- and a refusal would have turned a partial load
        # into NO load, which is the one outcome the fallback exists to
        # prevent.  The gap is named on the frame and the note travels with
        # the KPIs instead.

        df_processed = await loop.run_in_executor(None, _process_indicators_frame, df_final)

        # The records the caller needs are put back on the frame that is
        # returned: ``pandas.concat`` dropped them.
        if coverage_gap is not None:
            df_processed.attrs[COVERAGE_GAP_ATTR] = coverage_gap
        if difference is not None:
            df_processed.attrs[DIFFERENCE_ATTR] = difference
        df_processed.attrs[SOURCE_ATTR] = source or "unknown"
        df_processed.attrs[PROVENANCE_ATTR] = _provenance_record(
            source or "unknown",
            start_dt_utc,
            end_dt_utc,
            df_processed,
            differs_from_rest=bool(difference),
            difference=difference,
            interval=timeframe,
        )
        # THE NOTE IS ATTACHED HERE, ON THE FRAME, so every consumer of a
        # loader frame can read it -- the trainer, the data consumer, the
        # diagnostics routes, the API and the celery paths all get it, and
        # not only the two call sites that were instrumented by hand.
        _attach_klines_coverage_note(df_processed)

        logger.info(f"Loaded/merged and processed {len(df_final)} candles.")
        return df_processed

    except Exception as e:
        logger.error(
            f"Critical error in async download_klines for {symbol}: {e}", exc_info=True
        )
        return None


def get_aggtrade_base_path(symbol: str, market_type: str) -> Path:
    """Returns the root path to aggTrades partitions."""
    dummy_path = get_local_path(symbol, "aggTrades", market_type)
    return dummy_path.parent / "aggTrade"


async def download_agg_trades(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str = "futures_usdtm",
) -> Optional[pd.DataFrame]:
    """(ASYNCHRONOUS) Hybrid aggTrades loader with support for partitioned data."""
    if isinstance(start_dt, str):
        start_dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
    if isinstance(end_dt, str):
        end_dt = datetime.fromisoformat(end_dt.replace("Z", "+00:00"))

    loop = asyncio.get_running_loop()
    start_dt_utc = (
        start_dt.astimezone(timezone.utc)
        if start_dt.tzinfo
        else start_dt.replace(tzinfo=timezone.utc)
    )
    end_dt_utc = (
        end_dt.astimezone(timezone.utc)
        if end_dt.tzinfo
        else end_dt.replace(tzinfo=timezone.utc)
    )

    base_partition_path = get_aggtrade_base_path(symbol, market_type)
    df_local = None

    if base_partition_path.exists():
        logger.info(
            f"Found directory with aggTrades partitions: {base_partition_path}. Reading in a separate thread..."
        )
        try:
            read_func = functools.partial(
                pd.read_parquet,
                base_partition_path,
                engine="pyarrow",
                filters=[
                    ("timestamp", ">=", start_dt_utc),
                    ("timestamp", "<=", end_dt_utc),
                ],
            )
            df_local = await loop.run_in_executor(None, read_func)
            logger.info(f"Loaded from partitions {len(df_local)} aggTrades records.")
        except Exception as e:
            logger.warning(f"Error reading partitioned data: {e}.")
            df_local = pd.DataFrame()

    api_start_dt = start_dt_utc
    if df_local is not None and not df_local.empty:
        last_local_ts = df_local.index[-1]
        if last_local_ts >= end_dt_utc - pd.Timedelta(minutes=5):
            return df_local
        api_start_dt = last_local_ts + pd.Timedelta(milliseconds=1)
        logger.info(f"Local aggTrades are incomplete. Downloading from {api_start_dt}")
    else:
        logger.info("Local aggTrades data not found/empty. Full download via API.")

    df_api = await loop.run_in_executor(
        None,
        _download_agg_trades_from_api,
        symbol,
        api_start_dt,
        end_dt_utc,
        market_type,
    )

    if df_api is None:
        return df_local

    df_final = pd.concat([df_local, df_api]) if df_local is not None else df_api
    df_final.drop_duplicates(subset=["agg_trade_id"], keep="last", inplace=True)
    df_final.sort_index(inplace=True)

    return df_final


async def download_open_interest(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str = "futures_usdtm",
    timeframe: str = "5m",
) -> Optional[pd.DataFrame]:
    """(ASYNCHRONOUS) Hybrid open interest history loader."""
    try:
        if isinstance(start_dt, str):
            start_dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
        if isinstance(end_dt, str):
            end_dt = datetime.fromisoformat(end_dt.replace("Z", "+00:00"))

        loop = asyncio.get_running_loop()
        start_dt_utc = (
            start_dt.astimezone(timezone.utc)
            if start_dt.tzinfo
            else start_dt.replace(tzinfo=timezone.utc)
        )
        end_dt_utc = (
            end_dt.astimezone(timezone.utc)
            if end_dt.tzinfo
            else end_dt.replace(tzinfo=timezone.utc)
        )

        local_path = get_local_path(symbol, "open_interest", market_type)
        df_local = None

        if local_path.exists():
            logger.info(f"Found local open_interest file: {local_path}. Reading...")
            df_local = await loop.run_in_executor(None, pd.read_parquet, local_path)
            df_local = df_local[
                (df_local.index >= start_dt_utc) & (df_local.index <= end_dt_utc)
            ]

        api_start_dt = start_dt_utc
        if df_local is not None and not df_local.empty:
            last_local_ts = df_local.index[-1]
            if last_local_ts >= end_dt_utc - pd.Timedelta(minutes=10):
                return df_local
            api_start_dt = last_local_ts + pd.Timedelta(milliseconds=1)
            logger.info(
                f"Local open_interest data is incomplete. Downloading more with {api_start_dt}"
            )
        else:
            logger.info(
                "Local open_interest file not found/empty. Full download via API."
            )

        df_api = await loop.run_in_executor(
            None,
            _download_open_interest_from_api,
            symbol,
            timeframe,
            api_start_dt,
            end_dt_utc,
            market_type,
        )

        if df_api is None and (df_local is None or df_local.empty):
            return None

        df_final = pd.concat([df_local, df_api]) if df_local is not None else df_api
        if df_final.empty:
            return df_final

        df_final = df_final[~df_final.index.duplicated(keep="last")].sort_index()
        logger.info(f"Loaded/merged {len(df_final)} open interest records.")
        return df_final

    except Exception as e:
        logger.error(
            f"Critical error in async download_open_interest for {symbol}: {e}",
            exc_info=True,
        )
        return None


async def download_aggregated_depth(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str = "futures_usdtm",
) -> Optional[pd.DataFrame]:
    """
    (ASYNCHRONOUS) Loader for aggregated order book data.
    IMPORTANT: This function works ONLY with local data. It does not attempt to download them with API.
    Data must be pre-prepared and saved in Parquet format.
    """
    try:
        loop = asyncio.get_running_loop()
        start_dt_utc = (
            start_dt.astimezone(timezone.utc)
            if start_dt.tzinfo
            else start_dt.replace(tzinfo=timezone.utc)
        )
        end_dt_utc = (
            end_dt.astimezone(timezone.utc)
            if end_dt.tzinfo
            else end_dt.replace(tzinfo=timezone.utc)
        )

        local_path = get_local_path(symbol, "aggregated_depth", market_type)

        if local_path.exists():
            logger.info(f"Found local aggregated_depth file: {local_path}. Reading...")

            read_func = functools.partial(pd.read_parquet, local_path)
            df_local = await loop.run_in_executor(None, read_func)

            # Filter for date if the file contains more data than needed
            if not df_local.empty:
                df_local = df_local[
                    (df_local.index >= start_dt_utc) & (df_local.index <= end_dt_utc)
                ]

            if not df_local.empty:
                logger.info(
                    f"Loaded {len(df_local)} aggregated depth records from local file."
                )
                return df_local
            else:
                logger.warning(
                    f"Local file aggregated_depth {local_path} is empty for the requested range."
                )
                return pd.DataFrame()
        else:
            logger.warning(
                f"Local file for aggregated_depth not found for path: {local_path}. Returning an empty DataFrame."
            )
            return pd.DataFrame()

    except Exception as e:
        logger.error(
            f"Critical error in download_aggregated_depth for {symbol}: {e}",
            exc_info=True,
        )
        return None
