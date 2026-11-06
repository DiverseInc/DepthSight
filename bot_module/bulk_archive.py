# bot_module/bulk_archive.py
"""Klines fallback backed by Binance's public bulk archives.

WHY THIS EXISTS
---------------
The Binance REST market-data endpoints used by the loader
(``https://api.binance.com/api/v3`` and ``https://fapi.binance.com/fapi/v1``)
answer **HTTP 451** from networks that Binance geo/IP-blocks.  The body is a
short JSON document ("Service unavailable from a restricted location
according to 'b. Eligibility' in https://www.binance..."), not a transport
error, and it is not a bot-block: the same request with a browser
User-Agent still answers 451.  When the loader cannot reach them, every
backtest dies with "Failed to load historical data for <symbol>."

Binance publishes the *same* candles as downloadable archives on a different
host, ``data.binance.vision``, which stays reachable.  This module reads
those archives so the loader can still produce candles.

The venue, the symbol and the requested window do not change: these are
Binance's own candles for the same market.

ARCHIVE LAYOUT (measured against the live archives, not assumed)
---------------------------------------------------------------
    {BASE}/futures/um/monthly/klines/{SYM}/{TF}/{SYM}-{TF}-{YYYY-MM}.zip
    {BASE}/futures/um/daily/klines/{SYM}/{TF}/{SYM}-{TF}-{YYYY-MM-DD}.zip
    {BASE}/spot/monthly/klines/{SYM}/{TF}/{SYM}-{TF}-{YYYY-MM}.zip
    {BASE}/spot/daily/klines/{SYM}/{TF}/{SYM}-{TF}-{YYYY-MM-DD}.zip

Each ``.zip`` holds exactly ONE CSV with 12 positional columns.  Two measured
quirks are handled here because both are real and both break a naive parser:

1. **Some files have a header row, some do not.**  SPOT archives have NO
   header -- the first line is already data.  Futures UM archives carry the
   ``open_time,open,high,...`` header (verified at least as far back as
   2022-06).  The layout has varied over time, so the parser SNIFFS the
   first field instead of assuming a rule for a given venue or date.

2. **Timestamps are not always milliseconds.**  Binance switched the SPOT
   archives to MICROseconds on 2025-01-01 (spot 2024-12 = 13 digits, spot
   2025-01 = 16 digits).  Futures archives stay in milliseconds.  The unit is
   detected per file from the magnitude of the first timestamp and normalised
   back to milliseconds, so the resulting index is identical to the one the
   REST path produces.

The 12 archive columns are POSITIONALLY identical to the loader's
``KLINE_COLUMNS`` but are named differently by Binance
(``quote_volume``/``count``/``taker_buy_volume`` vs
``quote_asset_volume``/``number_of_trades``/``taker_buy_base_asset_volume``),
so the mapping is strictly by position.
"""

import contextvars
import io
import logging
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

import backoff
import pandas as pd
import requests
from requests.adapters import HTTPAdapter, Retry

from . import config

logger = logging.getLogger("bot_module.bulk_archive")

# Same fault-tolerance shape as the REST loader session.
retry_strategy = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[418, 429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http_session = requests.Session()
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

# Binance's own names for the 12 positional CSV fields.
ARCHIVE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]

# The loader's own naming for the six columns it keeps.
RESULT_COLUMNS = ["open", "high", "low", "close", "volume", "number_of_trades"]

# Epoch milliseconds are ~1.0e12 today and stay below 1e14 for the next
# century; epoch microseconds are ~1.0e15.  The threshold separates them.
_MICROSECOND_THRESHOLD = 10**14

# Binance publishes the daily klines archives with roughly a one day of lag
# and the monthly ones with roughly two days, so the trailing edge of a window
# that runs up to "now" legitimately has no file yet.  A missing daily file
# only PROVES anything once it is older than that lag -- see
# ``config.BULK_ARCHIVE_PUBLICATION_LAG_DAYS`` for the MEASURED values.
def _publication_lag() -> timedelta:
    """How long Binance takes to publish an archive, read from the config."""
    return timedelta(days=float(config.BULK_ARCHIVE_PUBLICATION_LAG_DAYS))


# The key under which the loader records the LEADING-EDGE COVERAGE GAP on the
# frame's ``attrs``.  The frame may cover a SHORTER period than the one that
# was asked for -- the archive holds only what Binance published -- so a
# caller that must not trade a shorter window than it requested needs to be
# able to SEE that it happened.  The value is a dict; see
# ``_leading_shortfall`` for its keys.
#
# ``DataFrame.attrs`` is used because it does not change the frame's shape,
# its columns or its values: the existing callers keep indexing the frame
# exactly as they do today.  The loader carries the key across its own
# operations explicitly (see ``data_loader.download_klines``), because
# ``pandas.concat`` DROPS the attrs when the frames it merges do not all
# carry the same ones.
COVERAGE_GAP_ATTR = "bulk_archive_coverage_gap"

# The key under which the loader records the DIFFERENCE between the frame
# the archive produced and the frame the REST path would have produced for
# the same request.
#
# The two are NOT the same frame, and the difference is not a curiosity:
#
# * the archive holds only CLOSED bars -- the REST path also returns the
#   bar that is still forming, so the archive's frame can be one bar
#   shorter at the trailing edge;
# * the archive's trailing edge LAGS the REST path's by the publication
#   lag, because Binance has not published the last days yet;
# * the archive's bars sit on Binance's own grid, which is not always the
#   grid the REST path uses (the offset is MEASURED from the delivered
#   bars, not assumed -- see ``_bar_grid_phase_hours``).
#
# A caller that asks for a window and receives archive data must be able to
# tell, from the API it is given, exactly what it got and what is missing.
# That is what this record is for, and the loader ENFORCES it: an
# archive-sourced frame that arrives without a truthful record never
# reaches a caller.
DIFFERENCE_ATTR = "bulk_archive_frame_difference"

# Binance's kline intervals are a COUNT and a UNIT.  The unit is CASE
# SENSITIVE: "m" is minutes and "M" is months, and the archive keeps that
# distinction.
_INTERVAL_UNITS = ("s", "m", "h", "d", "w", "M")


class BulkArchiveCoverageError(RuntimeError):
    """The archives cannot PROVE they cover the requested window.

    Raised when the frame the archive produced starts LATER than the
    requested window and the archive cannot prove that the symbol simply
    did not trade in the missing period -- the leading days it holds
    nothing for are still inside the publication lag, so their absence
    proves nothing at all.

    Returning such a frame would silently change the meaning of a
    backtest: the P&L would look valid while trading a window the caller
    never asked for.  A wrong number is worse than no number, so the load
    fails instead.

    It is NOT raised for a gap INSIDE the traded range any more: the data
    that exists is served with the gap recorded on the frame, because the
    caller can then see exactly what is missing and refuse the frame
    itself.
    """


class BulkArchiveDeadlineExceeded(RuntimeError):
    """The fallback ran out of time before it could cover the window.

    One archive request can burn a whole request timeout, and the trailing
    month of a window can need 31 daily files, so an unreachable archive
    host would otherwise stall the loader for MINUTES.  The fallback
    therefore carries a DEADLINE for the whole load (see ``_DEADLINE``) and
    this is raised when it is spent.  The load fails loudly: it never
    serves the candles it happened to collect before the deadline.
    """


# The deadline of the CURRENT fallback load, as an absolute
# ``time.monotonic()`` value.  ``None`` means "no deadline", and the request
# is then bounded only by the per-request timeout.
#
# It lives in a ContextVar rather than in a module global because the loader
# runs these loads in a THREAD POOL: two loads must never share one
# deadline.  A ContextVar is per thread, and the value is reset when the
# load ends, so a pooled thread never carries a deadline into the next load.
_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "bulk_archive_deadline", default=None
)

# The ``time.monotonic()`` value at which the CURRENT fallback load started.
# The ceiling of the budget is measured from it, so a load that buys itself
# more time cannot buy more than the ceiling allows.
_LOAD_START: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "bulk_archive_load_start", default=None
)

# Set when a transport failure has collapsed the budget of the running
# load.  Once it is set the load no longer BUYS budget: the host has been
# found not to answer, and every further request would be another wait on
# a host that is not there.
_COLLAPSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bulk_archive_collapsed", default=False
)

# How many requests the running load has made, and how many its window was
# EXPECTED to need.  The budget covers the expected ones; every request
# BEYOND the estimate buys one more slice (see ``_extend_deadline``).
_REQUESTS_MADE: contextvars.ContextVar[int] = contextvars.ContextVar(
    "bulk_archive_requests_made", default=0
)
_EXPECTED_REQUESTS: contextvars.ContextVar[int] = contextvars.ContextVar(
    "bulk_archive_requests_expected", default=0
)

# How many files have failed IN A ROW during the running load, with nothing
# in between that answered.  ONE file that was lost on the way is not proof
# that the host is gone -- a CDN edge that resets a single connection, a
# network that changes, or a proxy that drops one request all look the same
# from here -- but TWO in a row is.
#
# CORRECTION (2026-11-02): this used to be a plain counter of ATTEMPTS, and
# the word "IN A ROW" is doing the work now: a file is counted once however
# many times ``backoff`` retried it, and the count is of the files that
# failed since the last file that ANSWERED.

# How many CONSECUTIVE transport failures collapse the budget of a load.
# The second one: the first costs one file, the second ends the load.
_TRANSPORT_FAILURE_COLLAPSE_THRESHOLD = 2

# The files that could not be FETCHED since the last file that ANSWERED.
# Two different files in a row, with nothing in between that answered, is
# the host not serving -- which is what ends the load.  One file that was
# lost on the way is not.
_FAILED_SINCE_SUCCESS: contextvars.ContextVar[List[str]] = contextvars.ContextVar(
    "bulk_archive_failed_since_success", default=[]
)

# The archive files that could not be FETCHED during the running load, as
# opposed to the ones Binance has not published.  The difference record
# carries them, because "we could not read this day" is a different fact
# from "Binance did not trade this day".
_UNFETCHABLE_FILES: contextvars.ContextVar[List[str]] = contextvars.ContextVar(
    "bulk_archive_unfetchable_files", default=[]
)


def _expected_request_count(months: int) -> int:
    """How many archive requests a window of ``months`` months can need.

    One monthly archive per month, plus the daily files of the trailing
    month -- the month Binance has not published a monthly archive for yet.
    It is an ESTIMATE, and the load grants itself more budget for every
    request it actually makes (see ``_extend_deadline``), so an estimate
    that is too small cannot kill a legitimate window.
    """
    return months + 31


def _archive_budget_seconds(months: int) -> float:
    """The budget of ONE fallback load, SCALED to the size of the window.

    A FIXED budget cannot serve windows of every size: a two-year window is
    24 monthly archives plus up to 31 daily files, and on a normal link
    that is comfortably over a fixed two minutes.  The budget of a load is
    therefore the base budget PLUS one slice per request the window can
    need, capped by the ceiling from the configuration.
    """
    scaled = float(config.BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS) + (
        _expected_request_count(months)
        * float(config.BULK_ARCHIVE_PER_REQUEST_BUDGET_SECONDS)
    )
    return min(scaled, float(config.BULK_ARCHIVE_MAX_TOTAL_TIMEOUT_SECONDS))


def _extend_deadline() -> None:
    """Buys ONE more request's worth of budget, but only past the estimate.

    The budget is scaled to the window up front, and that scale covers the
    requests the window is EXPECTED to need (one monthly archive per month
    plus the daily files of the trailing month).  A window can need MORE
    than that: a symbol that was listed halfway through it has no monthly
    archive for any of the months before its listing, and every one of
    those months falls back to its daily files.

    Only the requests BEYOND the estimate buy budget.  If every request
    bought a slice, the budget would grow with the work and would never
    bind -- and the budget is what keeps a load from waiting for minutes
    on a host that is not answering.

    A load whose budget has been COLLAPSED (see ``_collapse_deadline``)
    buys nothing more: the host has been found not to answer.
    """
    if _COLLAPSED.get():
        return
    deadline = _DEADLINE.get()
    if deadline is None:
        return
    start = _LOAD_START.get()
    if start is None:
        return
    made = _REQUESTS_MADE.get() + 1
    _REQUESTS_MADE.set(made)
    if made <= _EXPECTED_REQUESTS.get():
        # The scaled budget already covers this one.
        return
    allowance = float(config.BULK_ARCHIVE_PER_REQUEST_BUDGET_SECONDS)
    ceiling = start + float(config.BULK_ARCHIVE_MAX_TOTAL_TIMEOUT_SECONDS)
    _DEADLINE.set(min(deadline + allowance, ceiling))


def _collapse_deadline() -> None:
    """Gives up on the archive host after a TRANSPORT failure.

    A transport failure -- a timeout, a reset connection, a dead host --
    is not "this one file is slow": the host is gone.  Without this the
    load would spend its whole (scaled) budget on a host that cannot
    answer, which is exactly the stall the deadline exists to prevent.

    The grace left is ONE REQUEST TIMEOUT, so the retry of a flaky file
    still gets its chance; after that the budget is spent and the load
    fails.  The budget is only ever SHRUNK here, and a collapsed load
    buys no more of it.

    CORRECTION (2026-11-02): the sentence above is the PREMISE this
    function was built on, and the premise is too strong.  "A transport
    failure ... is not 'this one file is slow': the host is gone" is not
    true of ONE failure: a CDN edge that resets a single connection, a
    laptop that changes network, or a proxy that drops one request all
    look exactly like a dead host from here.  One failure collapsing the
    WHOLE budget threw away every byte already downloaded because one
    file was unlucky.  The original wording is kept above so the
    correction can be read against the words it corrects.

    The fix keeps the protection the collapse exists for -- a host that
    really is gone must not be waited on for the whole budget -- while
    surviving the case it was throwing away.  A TIMEOUT still collapses the
    budget at once, because a host that did not answer inside its timeout
    is not there.  A connection that was RESET does not: it is evidence
    about one request, so the file is recorded as one that could not be
    fetched, the load keeps its budget, and the budget is collapsed only
    when a SECOND file fails as well (see ``_FAILED_SINCE_SUCCESS``).
    """
    deadline = _DEADLINE.get()
    if deadline is None:
        return
    grace = float(config.BULK_ARCHIVE_REQUEST_TIMEOUT_SECONDS)
    _DEADLINE.set(min(deadline, time.monotonic() + grace))
    _COLLAPSED.set(True)


def _note_transport_failure(url: str) -> int:
    """Records a file that could not be FETCHED, and says how many there are.

    A file is counted ONCE, however many times ``backoff`` retried it: the
    retries of one file are one failure, not several.

    The count is of the files that have failed SINCE THE LAST FILE THAT
    ANSWERED, because that is the question the count answers: two different
    files in a row, with nothing in between that answered, is the host not
    serving.  One file that was lost on the way is not.
    """
    failed = list(_FAILED_SINCE_SUCCESS.get())
    if url not in failed:
        failed.append(url)
    _FAILED_SINCE_SUCCESS.set(failed)
    _note_unfetchable_file(url)
    return len(failed)


def _note_transport_success(url: str) -> None:
    """Forgets the failures: the host answered, so it is alive."""
    _FAILED_SINCE_SUCCESS.set([])


def _note_unfetchable_file(url: str) -> None:
    """Records a file that could not be FETCHED, as opposed to one that is
    not published.

    These are two different facts and the difference record has to tell them
    apart: "Binance has not published this day" and "we could not read this
    day" are not the same thing, and a caller that is told the first when
    the second happened has been told a falsehood.
    """
    files = list(_UNFETCHABLE_FILES.get())
    if url not in files:
        files.append(url)
    _UNFETCHABLE_FILES.set(files)


def _is_host_not_answering(error: BaseException) -> bool:
    """True when the failure says the HOST is not answering.

    A TIMEOUT is evidence about the host: it did not answer inside the
    timeout it was given, so the host is not there and the load must not
    wait for it.  A connection that was RESET is evidence about ONE request
    -- a CDN edge that drops a connection, a network that changes, a proxy
    that refuses one request -- and says nothing about the next file.
    """
    return isinstance(error, requests.exceptions.Timeout)

# Binance's REST API accepts a couple of aliases the archive folders do not
# use.  The interval is never lower-cased: "1m" is one minute and "1M" is one
# month, and the archive keeps that distinction.
_INTERVAL_ALIASES = {
    "60m": "1h",
    "60min": "1h",
    "240m": "4h",
    "1440m": "1d",
    "1D": "1d",
    "1H": "1h",
}


# ==============================================================================
# URL construction
# ==============================================================================


def _archive_market_path(market_type: str) -> str:
    """Maps a loader ``market_type`` onto the archive's folder layout.

    This MUST mirror ``data_loader._perform_api_request`` exactly (the
    request behind ``data_loader._make_api_request``), because the archive
    is a FALLBACK for that REST path: for one request the two must
    answer with the same venue.  Both of them ask
    ``config.resolve_market_venue`` for the venue, so they cannot drift
    apart the way two hand-written lists can.

    Two things must never happen here:

    * a market type that merely CONTAINS "spot" must not get spot candles,
      because the REST path would have queried the futures host;
    * a COIN-M market type must not get USDT-M candles, and must not get
      COIN-M candles for a request the REST path answered with USDT-M
      ones either.  The venue is decided ONCE, by the market type, and
      both paths follow it.

    The venue names are the archive's own folders, MEASURED against the live
    archive host: ``spot``, ``futures/um`` and ``futures/cm`` all answer
    200 for a monthly archive.
    """
    venue = config.resolve_market_venue(market_type)
    return {
        "spot": "spot",
        "futures_coinm": "futures/cm",
        "futures_usdtm": "futures/um",
    }[venue]


def _normalise_interval(timeframe: str) -> str:
    """Maps a loader timeframe onto the archive's interval folder name."""
    tf = (timeframe or "").strip()
    return _INTERVAL_ALIASES.get(tf, tf)


def _monthly_url(
    symbol: str, interval: str, market_type: str, month_start: datetime
) -> str:
    """URL of the MONTHLY archive for one calendar month."""
    sym = symbol.upper()
    period = f"{month_start.year:04d}-{month_start.month:02d}"
    filename = f"{sym}-{interval}-{period}.zip"
    market_path = _archive_market_path(market_type)
    return (
        f"{config.BINANCE_BULK_ARCHIVE_DATA_URL}/{market_path}/monthly/klines/"
        f"{sym}/{interval}/{filename}"
    )


def _daily_url(symbol: str, interval: str, market_type: str, day: datetime) -> str:
    """URL of the DAILY archive for one calendar day.

    The file name shape differs from the monthly one: ``...-1h-2024-01-15.zip``
    versus ``...-1h-2024-01.zip``.  They must not be mixed up.
    """
    sym = symbol.upper()
    period = f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
    filename = f"{sym}-{interval}-{period}.zip"
    market_path = _archive_market_path(market_type)
    return (
        f"{config.BINANCE_BULK_ARCHIVE_DATA_URL}/{market_path}/daily/klines/"
        f"{sym}/{interval}/{filename}"
    )


# ==============================================================================
# Download
# ==============================================================================


def _request_timeout() -> float:
    """The timeout for the NEXT archive request, bounded by the deadline.

    Raises :class:`BulkArchiveDeadlineExceeded` when the load has no time
    left, so a request is never started that cannot finish inside the
    remaining budget.
    """
    deadline = _DEADLINE.get()
    if deadline is None:
        return float(config.BULK_ARCHIVE_REQUEST_TIMEOUT_SECONDS)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BulkArchiveDeadlineExceeded(
            "The bulk archive fallback ran out of time after "
            f"{float(config.BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS):.0f}s."
        )
    return max(
        0.1,
        min(float(config.BULK_ARCHIVE_REQUEST_TIMEOUT_SECONDS), remaining),
    )


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=5,
    max_time=60,
    logger=logger,
)
def fetch_archive_bytes(url: str) -> Optional[bytes]:
    """Downloads one archive.

    Returns ``None`` when Binance has not published that file (HTTP 404),
    which is the normal case for a symbol/period that does not exist.  Any
    other failure raises and is retried by ``backoff`` exactly like the REST
    loader does.

    The request's timeout is bounded by the DEADLINE of the running load
    (see ``_DEADLINE``): a request that cannot finish inside the remaining
    budget is not started, and a load that is out of time raises
    :class:`BulkArchiveDeadlineExceeded`, which ``backoff`` does not retry.

    Every request that is ACTUALLY MADE buys the load one more slice of
    budget (``_extend_deadline``), so a window that needs more requests
    than the estimate gets them.  A transport failure collapses the budget
    (``_collapse_deadline``), because a host that does not answer must not
    be waited on for the whole budget.

    CORRECTION (2026-11-02): the collapse does NOT happen on every failure.
    The sentence above is kept so the correction can be read against the
    words it corrects, and it is too strong in two ways.  First, ONE failed
    request is not proof that the host is gone: a CDN edge that resets one
    connection, a network that changes, or a proxy that drops one request
    all look the same from here, and collapsing the budget on one of them
    threw away every byte the load had already downloaded because one file
    was unlucky.  Second, this function is what ``backoff`` RETRIES, so a
    collapse here fired on the FIRST ATTEMPT.

    The failure now says WHICH it is.  A TIMEOUT is evidence about the
    HOST -- it did not answer inside its timeout, so it is not there -- and
    it collapses the budget, which is what stops the retry chain and ends
    the load fast.  A connection that was RESET is evidence about ONE
    request: the file is recorded as one that could not be fetched, the
    load keeps its budget and carries on, and the budget is collapsed only
    when a SECOND file fails as well.
    """
    _extend_deadline()
    try:
        response = http_session.get(url, timeout=_request_timeout())
        if response.status_code == 404:
            logger.debug(f"Bulk archive not published: {url}")
            return None
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        if _is_host_not_answering(error):
            # The host did not answer inside the timeout it was given: it is
            # not there.  The budget is collapsed HERE, inside the function
            # ``backoff`` retries, so the retry chain stops at once instead
            # of burning the whole budget on a host that is gone.
            _collapse_deadline()
        raise
    return response.content


# ==============================================================================
# Parsing
# ==============================================================================


def _empty_result() -> pd.DataFrame:
    """An empty frame with the same shape as a populated one."""
    frame = pd.DataFrame({name: pd.Series(dtype="float64") for name in RESULT_COLUMNS})
    frame.index = pd.DatetimeIndex([], tz="UTC", name="open_time")
    return frame


def _parse_archive_csv(text: str) -> pd.DataFrame:
    """Parses one archive CSV into the loader's klines frame.

    Mirrors ``data_loader._download_klines_from_api`` exactly: same index
    (tz-aware UTC, named ``open_time``), same columns, same dtypes.
    """
    # Some archives start with a header row, some start straight into data.
    first_line = text.split("\n", 1)[0]
    first_field = first_line.split(",")[0].strip().strip('"')
    try:
        int(first_field)
        skiprows = 0
    except ValueError:
        skiprows = 1

    if not text.strip() or len(text.strip().splitlines()) <= skiprows:
        # A file with no rows at all: the symbol did not trade that day.
        return _empty_result()

    try:
        frame = pd.read_csv(
            io.StringIO(text),
            header=None,
            skiprows=skiprows,
            dtype=str,
        )
    except pd.errors.EmptyDataError:
        # A file with nothing in it: the symbol did not trade that day.
        return _empty_result()
    if frame.empty:
        return _empty_result()

    frame = frame.iloc[:, : len(ARCHIVE_COLUMNS)]
    frame.columns = ARCHIVE_COLUMNS[: frame.shape[1]]

    # Timestamps: milliseconds in futures, microseconds in spot since
    # 2025-01-01.  Normalise everything back to milliseconds.
    open_time = pd.to_numeric(frame["open_time"], errors="coerce")
    valid = open_time.notna()
    if not valid.any():
        return _empty_result()
    frame = frame[valid].copy()
    open_time = open_time[valid]
    if abs(int(open_time.iloc[0])) >= _MICROSECOND_THRESHOLD:
        open_time = open_time // 1000
    frame["open_time"] = open_time

    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame = frame.set_index("open_time")

    # "count" is Binance's name for the loader's "number_of_trades".
    frame["number_of_trades"] = pd.to_numeric(frame["count"], errors="coerce")
    frame[RESULT_COLUMNS] = frame[RESULT_COLUMNS].apply(pd.to_numeric, errors="coerce")
    frame = frame[~frame.index.duplicated(keep="first")]
    return frame[RESULT_COLUMNS]


def parse_archive_candles(zip_bytes: bytes) -> pd.DataFrame:
    """Turns one archive ``.zip`` into the loader's klines DataFrame."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            members = [name for name in archive.namelist() if not name.endswith("/")]
        if not members:
            raise ValueError("Bulk archive contains no CSV member.")
        with archive.open(members[0]) as handle:
            text = handle.read().decode("utf-8-sig")
    return _parse_archive_csv(text)


# ==============================================================================
# Window walking
# ==============================================================================


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _add_month(month_start: datetime, count: int = 1) -> datetime:
    if count == 0:
        return month_start
    total = month_start.year * 12 + (month_start.month - 1) + count
    return month_start.replace(year=total // 12, month=total % 12 + 1)


def _parse_interval(interval: str) -> Tuple[int, str]:
    """Splits a kline interval into its COUNT and its UNIT.

    "3d" -> (3, "d"), "1M" -> (1, "M"), "15m" -> (15, "m").  An interval
    that does not parse falls back to (1, ""), which every caller below
    treats as "one bar a day" -- the behaviour every supported timeframe
    had before the bucket size was derived from the interval.
    """
    text = (interval or "").strip()
    if len(text) < 2:
        return 1, ""
    unit = text[-1]
    if unit not in _INTERVAL_UNITS:
        return 1, ""
    try:
        count = int(text[:-1])
    except ValueError:
        return 1, ""
    if count <= 0:
        return 1, ""
    return count, unit


def interval_duration(interval: str) -> timedelta:
    """The nominal duration of ONE bar of ``interval``.

    A calendar month has no fixed length, so "1M" uses the SHORTEST
    month (28 days): that is the smallest duration a monthly bar can
    cover, and using it keeps the leading-edge rule conservative.

    The loader measures a frame's trailing edge with the SAME arithmetic,
    so the two can never disagree about how long a bar covers.
    """
    count, unit = _parse_interval(interval)
    if unit == "M":
        return timedelta(days=28)
    if unit == "w":
        return timedelta(days=7 * count)
    if unit == "d":
        return timedelta(days=count)
    if unit == "h":
        return timedelta(hours=count)
    if unit == "m":
        return timedelta(minutes=count)
    if unit == "s":
        return timedelta(seconds=count)
    return timedelta(days=1)


# Binance's WEEKLY klines open on Monday and its MONTHLY ones open on the
# first of the month, so the calendar bucket a bar belongs to depends on the
# interval.  For every interval up to one day it is the calendar day the bar
# opens in.
def _bucket_bounds(day: datetime, interval: str) -> Tuple[datetime, datetime]:
    """The half-open calendar bucket ``day`` belongs to for ``interval``.

    The bucket size is DERIVED FROM THE INTERVAL, not from a list of
    hard-coded strings: a "3d" interval gets three-day buckets, "1w" gets
    seven-day buckets and "1M" gets the calendar month.  Before, every
    interval that was not "1M" or "1w" fell through to a single calendar
    day, so a multi-day interval demanded a bar on EVERY calendar day and
    reported holes that were not holes.

    Every interval the product uses today (1m, 5m, 15m, 1h, 4h, 1d) keeps
    the calendar day it had before, so no supported timeframe changes.
    """
    count, unit = _parse_interval(interval)
    if unit == "M":
        start = day.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, _add_month(start, count)
    if unit == "w":
        start = (day - timedelta(days=day.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, start + timedelta(days=7 * count)
    if unit == "d" and count > 1:
        # A multi-day grid, aligned to the UNIX epoch so the buckets tile
        # the calendar without gaps or overlaps.
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        epoch_day = (day - epoch).days
        start_day = epoch_day - (epoch_day % count)
        start = epoch + timedelta(days=start_day)
        return start, start + timedelta(days=count)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _iter_months(start_dt: datetime, end_dt: datetime) -> Iterator[datetime]:
    """Yields the first instant of every calendar month the window touches."""
    month = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= end_dt:
        yield month
        month = _add_month(month)


def _absence_is_provable(day: datetime, now: datetime) -> bool:
    """True when the ABSENCE of ``day``'s file PROVES there was no trading.

    Binance publishes a daily archive for every day a symbol traded, with a
    lag of about a day (MEASURED: see
    ``config.BULK_ARCHIVE_PUBLICATION_LAG_DAYS``).  Once a day is older than
    that lag, its file would exist if the symbol had traded, so its absence
    is evidence that the symbol did not trade that day.

    A day that is still INSIDE the lag proves nothing: the file may simply
    not be published yet.  That distinction is what separates a trailing
    edge that is short for a good reason from a leading edge that is short
    for an unknown one.
    """
    return (day + timedelta(days=1)) <= (now - _publication_lag())


def _is_missing_day_a_hole(day: datetime, now: datetime) -> bool:
    """True when a missing daily file for ``day`` is a genuine HOLE.

    The trailing edge of a window that runs to "now" legitimately has no file
    yet, because Binance publishes the daily archives with about a day of
    lag.  A day that ended well before that lag window must have a file.
    """
    return _absence_is_provable(day, now)


def _is_missing_bucket_a_hole(
    bucket_start: datetime,
    bucket_end: datetime,
    first_bar: datetime,
    last_bar: datetime,
    now: datetime,
) -> bool:
    """True when a calendar bucket that holds NO data is a genuine HOLE.

    ``bucket_start``/``bucket_end`` are the half-open bounds of the bucket:
    a calendar day for every interval up to a day, a week for "1w" and a
    month for "1M".  The rules are the ones the absent-DAILY-file path
    already uses, applied to the whole bucket:

    * the bucket is inside the publication lag -> the trailing edge of a
      window that runs to "now" legitimately has no data yet;
    * the bucket is entirely before the first observed bar -> the symbol was
      not listed yet (or was delisted and relisted); there is no hole, the
      history simply starts later;
    * the bucket is entirely after the last observed bar -> delisted, or the
      trailing publication lag; there is no hole;
    * otherwise -> the symbol traded on both sides of the bucket, so the
      data should have existed.  If it did not, the archive has a hole in it
      and the load must fail rather than ship a gapped frame.
    """
    if not _is_missing_day_a_hole(bucket_start, now):
        return False
    if bucket_end <= first_bar:
        # Entirely before the first observed bar: not listed yet.
        return False
    if bucket_start > last_bar:
        # Entirely after the last observed bar: delisted / trailing edge.
        return False
    return True


def _is_absent_day_a_hole(
    day: datetime, first_bar: datetime, last_bar: datetime, now: datetime
) -> bool:
    """True when an ABSENT daily file for ``day`` is a genuine HOLE.

    An absent file only means "there is a hole" when the symbol demonstrably
    existed on that day.  Judged against the bars actually loaded for this
    window:

    * before the first observed bar -> the symbol was not listed yet (or was
      delisted and relisted); there is no hole, the history simply starts
      later;
    * after the last observed bar -> delisted, or the trailing publication
      lag; there is no hole;
    * in between -> the symbol traded on both sides of the day, so the file
      should have existed.  If it did not, the month has a hole in it and
      the load must fail rather than ship a gapped frame.

    A day missing from INSIDE a monthly archive is judged by exactly the
    same rule -- see ``_is_missing_bucket_a_hole``.
    """
    return _is_missing_bucket_a_hole(
        day, day + timedelta(days=1), first_bar, last_bar, now
    )


# ==============================================================================
# The leading edge of the window
# ==============================================================================


def _previous_bar_open(first_bar: datetime, interval: str) -> datetime:
    """The bar-open IMMEDIATELY BEFORE ``first_bar`` on the interval's grid.

    The grid is the one the ARCHIVE itself uses, so the answer is right
    whatever phase Binance aligns its bars to.  For a calendar-month
    interval the previous bar is the first of the previous month.

    MEASURED on the live archives on 2026-11-02, the spot and the USDT-M
    daily, weekly and three-day bars all open on the UTC grid (00:00 UTC,
    Monday 00:00 UTC and the epoch-aligned three-day grid), so the phase
    of the real archives is 0.  An earlier version of this comment claimed
    that a spot daily bar opens at 00:00 UTC+08:00: that was an ASSUMPTION
    about Binance's data that nobody had checked, and the live archives do
    not bear it out.  The phase that actually matters is measured from the
    delivered bars instead (see ``_bar_grid_phase_hours``).
    """
    count, unit = _parse_interval(interval)
    if unit == "M":
        month_start = first_bar.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return _add_month(month_start, -count)
    return first_bar - interval_duration(interval)


def _leading_shortfall(
    requested_start: datetime,
    first_bar: datetime,
    interval: str,
    absent_days: List[datetime],
    leading_month_published: Optional[bool],
    leading_month: Optional[datetime],
    now: datetime,
) -> Optional[Dict[str, Any]]:
    """The SHORTFALL at the leading edge of the window, or ``None``.

    A frame may cover a SHORTER period than the one that was asked for.
    That is normal and legitimate in one case: the symbol was listed
    later, and the archive holds only the history that exists.  It is
    DANGEROUS in another: the archives for the leading month were never
    published, so the archive cannot prove that the symbol simply did not
    trade then -- it might be that the data for that month is missing
    altogether, and the frame would silently start later than the window.

    The two are told apart by EVIDENCE, and the evidence is the publication
    lag:

    * the leading month's monthly archive WAS published -> Binance's own
      answer for that month.  The frame starts where the symbol started
      trading, which is exactly where the REST path would have started it.
      The shortfall is PROVEN to be a listing;
    * the archive holds no file for the leading days, and every one of
      those days is OLDER than the publication lag -> Binance would have
      published those files if the symbol had traded, so their absence
      PROVES it did not trade then.  The shortfall is PROVEN to be a
      listing as well;
    * the archive holds no file for the leading days and some of them are
      still INSIDE the publication lag -> their absence proves NOTHING.
      The frame would silently start later than the window, and that is
      UNPROVEN.  The load must not serve it: the shortfall is returned
      with ``proven`` False and the caller refuses it.

    The gap itself is measured on the archive's own bar grid: the bar
    that would come immediately before ``first_bar`` decides it.  If that
    bar would fall inside the requested window, it should have been
    returned and was not -- that is the gap.  If it would fall OUTSIDE
    the window, the frame starts exactly where the REST path would have
    started it, and there is nothing to report.
    """
    if leading_month_published:
        # Binance's own answer for the leading month.  The frame starts
        # where the symbol started trading, which is exactly where the REST
        # path would have started it too, so there is no coverage question
        # here: the REST frame would be short in the same way.
        return None

    previous_bar = _previous_bar_open(first_bar, interval)
    if previous_bar < requested_start:
        # The bar before the first one falls outside the requested window,
        # so the frame starts exactly where the REST path starts it.
        return None

    first_day = first_bar.replace(hour=0, minute=0, second=0, microsecond=0)
    # The days inside the requested window that the archive holds nothing
    # for.  The daily path only ever asks for the days that OVERLAP the
    # window, so an absent day before the first bar is a day of the window
    # the archive is missing.
    missing_leading_days = [
        day
        for day in absent_days
        if day < first_day and (day + timedelta(days=1)) > requested_start
    ]
    unprovable = [
        day for day in missing_leading_days if not _absence_is_provable(day, now)
    ]

    if not missing_leading_days:
        proven = True
        reason = (
            "the archive holds published files for the leading period, so it is "
            "Binance's own answer for it"
        )
    elif unprovable:
        proven = False
        reason = (
            f"{len(unprovable)} of the leading days the archive holds nothing "
            "for are still inside the publication lag, so the archive cannot "
            "prove that the symbol did not trade then; the frame would start "
            "later than the requested window"
        )
    else:
        proven = True
        reason = (
            "the archive holds no file for the leading days, and every one of "
            "them is older than the publication lag, so their absence proves "
            "the symbol did not trade then"
        )

    gap = first_bar - requested_start
    return {
        "requested_start": requested_start.isoformat(),
        "first_bar": first_bar.isoformat(),
        "gap_days": gap.days,
        "gap_seconds": gap.total_seconds(),
        "leading_month": (
            leading_month.strftime("%Y-%m") if leading_month is not None else None
        ),
        "leading_month_published": leading_month_published,
        "missing_leading_days": [
            day.strftime("%Y-%m-%d") for day in missing_leading_days
        ],
        "proven": proven,
        "reason": reason,
    }


def frame_difference(frame: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """The record of HOW the archive's frame differs from the REST frame.

    ``None`` means "this frame did not come from the archive".  The record
    is attached to the frame the archive returns (see ``DIFFERENCE_ATTR``),
    so it travels with the data instead of living in a log line a caller
    may never read.
    """
    if frame is None:
        return None
    difference = frame.attrs.get(DIFFERENCE_ATTR)
    if isinstance(difference, dict):
        return dict(difference)
    return None


def _rest_grid_open(moment: datetime, interval: str) -> datetime:
    """The bar open the REST path's grid gives for ``moment``.

    The REST path's grid is the one Binance's klines API uses: the calendar
    day for "1d", the Monday for "1w", the calendar month for "1M", an
    epoch-aligned multi-day grid for "3d", and an epoch-aligned multiple of
    the interval for everything shorter.
    """
    count, unit = _parse_interval(interval)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if unit == "M":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "w":
        return (moment - timedelta(days=moment.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if unit == "d":
        if count > 1:
            epoch_day = (moment - epoch).days
            return epoch + timedelta(days=epoch_day - (epoch_day % count))
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = count * {"s": 1, "m": 60, "h": 3600}.get(unit, 60)
    steps = int((moment - epoch).total_seconds()) // seconds
    return epoch + timedelta(seconds=steps * seconds)


def _bar_grid_phase_hours(frame: pd.DataFrame, interval: str) -> float:
    """The offset, in hours, between the ARCHIVE's bar grid and the REST one.

    MEASURED from the bars that were actually delivered, never assumed.
    The archives put their bars on Binance's own grid and that grid is not
    guaranteed to be the one the REST path uses, so a non-zero phase means
    the archive's bars open at a different moment than the REST path's and
    the two frames do not line up bar for bar.

    (MEASURED on the live archives on 2026-11-02: the spot and the USDT-M
    1d, 1w and 3d bars all open on the UTC grid -- 00:00 UTC, Monday
    00:00 UTC, and the epoch-aligned three-day grid -- so the phase of the
    real archives is 0.0 hours.  It is measured rather than assumed anyway,
    because an assumption here would be a claim about Binance's data that
    nobody checked.)
    """
    if frame.empty:
        return 0.0
    first = frame.index[0].to_pydatetime()
    expected = _rest_grid_open(first, interval)
    return (first - expected).total_seconds() / 3600.0


def _difference_record(
    symbol: str,
    timeframe: str,
    market_type: str,
    interval: str,
    requested_start: datetime,
    requested_end: datetime,
    frame: pd.DataFrame,
    shortfall: Optional[Dict[str, Any]],
    missing_buckets: List[Tuple[datetime, datetime]],
) -> Dict[str, Any]:
    """The record of HOW the archive's frame differs from the REST frame.

    It is attached to the frame the archive returns, and the loader ENFORCES
    it: an archive-sourced frame that arrives without a truthful record
    never reaches a caller.  A difference that only lives in a log line
    cannot be enforced, and one that a caller can forget to read is not a
    difference the caller was told about.

    The numbers in it are MEASURED from the frame that was delivered, not
    asserted: the trailing edge is the difference between the window that
    was asked for and the end of the LAST CLOSED BAR the archive could
    supply, and the grid phase is read off the bars themselves.
    """
    last_bar = frame.index[-1].to_pydatetime()
    # The window the frame covers ends with the last bar's CLOSE, not with
    # its open.
    covered_end = last_bar + interval_duration(interval)
    trailing_short = max(0.0, (requested_end - covered_end).total_seconds())
    phase = _bar_grid_phase_hours(frame, interval)
    shift = None
    if phase:
        shift = {
            "shift_hours": phase,
            "description": (
                f"the archive's bars open {phase:+.1f}h off the grid the REST "
                "path uses, so the two frames do not line up bar for bar"
            ),
        }
    return {
        "source": "bulk_archive",
        "symbol": symbol,
        "timeframe": timeframe,
        "market_type": market_type,
        "interval": interval,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": frame.index[0].isoformat(),
        "actual_end": last_bar.isoformat(),
        "rows": int(len(frame)),
        "closed_bars_only": True,
        "trailing_edge_short_by_seconds": trailing_short,
        "publication_lag_days": float(config.BULK_ARCHIVE_PUBLICATION_LAG_DAYS),
        "bar_grid_phase_hours": phase,
        "day_boundary_shift": shift,
        "leading_shortfall": shortfall,
        "missing_buckets": [
            {"start": bucket[0].isoformat(), "end": bucket[1].isoformat()}
            for bucket in missing_buckets
        ],
        "unfetchable_files": list(_UNFETCHABLE_FILES.get()),
        "differs_from_rest": True,
        "reason": (
            "the archive holds only CLOSED bars (the REST path also returns the "
            "bar that is still forming), its trailing edge lags the REST path's "
            "by the publication lag, and it can be missing days the REST path "
            "has; the numbers above say by how much"
        ),
    }


def coverage_gap(frame: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """The leading-edge coverage gap recorded on ``frame``, if there is one.

    ``None`` means "no leading-edge coverage gap was detected".  The
    returned dict carries the requested start, the first bar the archive
    could supply, the size of the gap in days and why it was reported.

    ``proven`` says whether the archive could PROVE that the symbol simply
    did not trade in the missing period (a listing) or not (the coverage of
    the leading edge is unknown).  An unproven gap never reaches a caller:
    the load fails instead.
    """
    if frame is None:
        return None
    gap = frame.attrs.get(COVERAGE_GAP_ATTR)
    if isinstance(gap, dict):
        return dict(gap)
    return None


class _MonthlyArchive(NamedTuple):
    """The outcome of ONE monthly archive request.

    The two outcomes are kept apart on purpose, because they mean
    different things:

    * ``published is False`` -- Binance has NOT published that monthly
      archive (HTTP 404).  Nothing is known about that month: the symbol
      may never have traded then, or the archive may simply be missing.
      The two are indistinguishable from here, which is exactly why a
      window that STARTS in such a month cannot prove it covers the
      window it was asked for.
    * ``published is True`` -- Binance HAS published that monthly
      archive.  Then the archive is Binance's own answer for that month:
      either it holds bars, and they start wherever the symbol started
      trading, or it is empty, and the symbol did not trade that month.
      Either way the month is accounted for and there is no gap.
    """

    frame: Optional[pd.DataFrame]
    published: bool


def _load_monthly_archive(
    symbol: str, interval: str, market_type: str, month_start: datetime
) -> _MonthlyArchive:
    """Fetches one monthly archive.

    ``published`` tells the caller whether Binance published that month at
    all; ``frame`` holds its candles (``None`` when it holds none).
    """
    url = _monthly_url(symbol, interval, market_type, month_start)
    try:
        payload = fetch_archive_bytes(url)
    except requests.exceptions.RequestException as error:
        if _is_host_not_answering(error):
            # The host is not there: the budget is already collapsed and the
            # load ends here.
            raise
        # ONE file that could not be fetched is not the host being gone: the
        # budget is kept, the month is served from its DAILY files instead,
        # and the file is RECORDED as one that could not be fetched (which is
        # a different fact from a month Binance has not published).  A
        # SECOND file that fails as well, with nothing in between that
        # answered, is the host not serving, and the load ends.
        failures = _note_transport_failure(url)
        if failures >= _TRANSPORT_FAILURE_COLLAPSE_THRESHOLD:
            _collapse_deadline()
            raise
        logger.warning(
            f"Bulk archive: the monthly archive {url} could not be fetched. "
            "The budget is kept and the month is served from its daily files."
        )
        return _MonthlyArchive(None, False)
    _note_transport_success(url)
    if payload is None:
        # NOT PUBLISHED: this month is simply unknown.
        return _MonthlyArchive(None, False)
    frame = parse_archive_candles(payload)
    if frame.empty:
        # PUBLISHED but empty: Binance says the symbol did not trade that
        # month.  That is an answer, not a hole.
        logger.warning(f"Bulk archive: monthly archive is empty {url}")
        return _MonthlyArchive(None, True)
    logger.info(f"Bulk archive: loaded monthly archive {url}")
    return _MonthlyArchive(frame, True)


def _load_daily_archives(
    symbol: str,
    interval: str,
    market_type: str,
    start_dt: datetime,
    end_dt: datetime,
    month_start: datetime,
    next_month: datetime,
) -> Tuple[List[pd.DataFrame], List[datetime]]:
    """Fetches the daily archives of one month that overlap the window.

    Returns the parsed frames and the days whose archive was ABSENT (HTTP
    404, i.e. Binance has not published that file).

    A file that EXISTS but holds no candle is NOT an absent day: that is
    Binance saying the symbol did not trade that day, which is legitimate
    data, not a publication hole.  Only an absent file can be a hole.
    """
    frames: List[pd.DataFrame] = []
    absent_days: List[datetime] = []
    day = month_start
    while day < next_month:
        if day <= end_dt and (day + timedelta(days=1)) > start_dt:
            url = _daily_url(symbol, interval, market_type, day)
            try:
                payload = fetch_archive_bytes(url)
            except requests.exceptions.RequestException as error:
                if _is_host_not_answering(error):
                    # The host is not there: the budget is already collapsed
                    # and the load ends here.
                    raise
                # ONE file that could not be fetched is not the host being
                # gone.  The day is counted as ABSENT -- the frame has a
                # hole there, and the hole is named on the frame -- and the
                # load keeps its budget and carries on.  A SECOND file that
                # fails as well, with nothing in between that answered, is
                # the host not serving, and the load ends.
                failures = _note_transport_failure(url)
                if failures >= _TRANSPORT_FAILURE_COLLAPSE_THRESHOLD:
                    _collapse_deadline()
                    raise
                logger.warning(
                    f"Bulk archive: the daily file {url} could not be fetched. "
                    "The budget is kept and the day is counted as absent."
                )
                absent_days.append(day)
                day += timedelta(days=1)
                continue
            _note_transport_success(url)
            if payload is None:
                absent_days.append(day)
                logger.debug(f"Bulk archive: daily file not published {url}")
            else:
                frame = parse_archive_candles(payload)
                if frame.empty:
                    # The file exists: the symbol did not trade that day.
                    logger.debug(f"Bulk archive: daily archive has no candles {url}")
                else:
                    frames.append(frame)
        day += timedelta(days=1)
    return frames, absent_days


def _missing_buckets_in_month(
    frame: pd.DataFrame,
    interval: str,
    month_start: datetime,
    next_month: datetime,
    window_start: datetime,
    window_end: datetime,
) -> List[Tuple[datetime, datetime]]:
    """The calendar buckets this month SHOULD hold but does NOT.

    A MONTHLY archive can be incomplete in exactly the way a daily file can
    be absent: Binance can publish a monthly archive that is missing whole
    days of its own month.  Nothing in the daily path would notice -- the
    monthly archive is used as it is -- so the missing days are found HERE
    and refused the same way an absent daily file is refused.

    Only the buckets the WINDOW touches are looked at, and only a bucket
    that holds no bar at all counts: a day inside the traded range with no
    bar in it is the same defect as an absent daily file.
    """
    present = set()
    for stamp in frame.index:
        present.add(_bucket_bounds(stamp.to_pydatetime(), interval)[0])

    missing: List[Tuple[datetime, datetime]] = []
    seen: set = set()
    day = month_start
    while day < next_month:
        if day <= window_end and (day + timedelta(days=1)) > window_start:
            bounds = _bucket_bounds(day, interval)
            # A multi-day bucket is visited once per day it covers, so the
            # same bucket is only reported once.
            if bounds[0] not in present and bounds[0] not in seen:
                seen.add(bounds[0])
                missing.append(bounds)
        day += timedelta(days=1)
    return missing


# ==============================================================================
# Public entry point
# ==============================================================================


def fetch_klines_from_bulk_archive(
    symbol: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str = "futures_usdtm",
) -> Optional[pd.DataFrame]:
    """Loads klines for ``[start_dt, end_dt]`` from Binance's bulk archives.

    Every month in the window is read from its MONTHLY archive (one download
    each) and clipped to the requested window, so a window that ends
    mid-month costs the same single request as a window that covers it whole.
    Only when a monthly archive has not been published yet -- the trailing
    month, while Binance is still publishing -- are the DAILY archives of the
    overlapping days used instead.

    MISSING DATA IS SERVED AND NAMED, NOT FATAL.  A day the archive holds
    nothing for, inside the range the symbol demonstrably traded through,
    is a real gap -- but it is not a reason to throw away the candles that
    DO exist.  The gap is reported at ERROR level and RECORDED on the
    frame, so the caller is told exactly which buckets are missing and can
    refuse the frame itself.

    A PUBLISHED MONTHLY ARCHIVE IS HELD TO THE SAME RULE.  Binance can
    publish a monthly archive that is missing whole days of its own month,
    and no absent daily file would ever report that: the monthly archive is
    used as it is.  A calendar bucket the window touches that holds no bar at
    all is therefore reported the same way.

    A day before the first observed bar is NOT a gap: the symbol was not
    listed yet, and the window simply carries the shorter history the
    archive holds.  A day after the last observed bar is not a gap either
    (delisted, or the trailing publication lag).

    THE WHOLE FALLBACK RUNS UNDER A DEADLINE THAT IS SCALED TO THE WINDOW.
    One archive request can burn a whole request timeout and the trailing
    month of a window can need 31 daily files, so an unreachable archive
    host would stall the loader for MINUTES.  The load therefore gets a
    budget (``_archive_budget_seconds``) that is the base budget PLUS one
    slice per request the window can need, so a two-year window is never
    killed by its own size.  Every request that is actually made buys one
    more slice, up to the ceiling; a host that stops answering gets the
    budget collapsed instead.  When the budget is spent the load fails
    loudly with :class:`BulkArchiveDeadlineExceeded` and NOTHING is served
    -- never the candles collected before the deadline.

    THE FRAME DIFFERS FROM THE REST FRAME, AND THE DIFFERENCE IS PART OF
    THE FRAME.  The archive holds only CLOSED bars (the REST path also
    returns the still-forming one), its trailing edge lags the REST path's
    by the publication lag, and it can be missing days the REST path has.
    Every one of those differences is MEASURED and recorded on the frame's
    ``attrs`` under the key ``DIFFERENCE_ATTR``
    ("bulk_archive_frame_difference"), and the loader ENFORCES the record:
    an archive-sourced frame that arrives without a truthful record never
    reaches a caller.  Read it with ``bulk_archive.frame_difference(frame)``,
    which returns ``None`` for a frame that did not come from the archive.

    THE FRAME MAY COVER A SHORTER PERIOD THAN THE ONE REQUESTED.  The
    archive holds what Binance published, and Binance publishes nothing
    for a symbol that did not trade, so a window that starts before the
    symbol was listed comes back starting later.  That is normal, and the
    load does not fail for it -- but it must never happen UNSEEN, because a
    backtest asked for 2024-01-01..2025-12-31 that silently receives a
    frame starting 2024-02-01 reports the KPIs of the shorter window as if
    they were the requested ones.

    So the loader MEASURES the leading edge and PROVES it:

    * when the leading month's monthly archive WAS published, the frame
      starts wherever the symbol started trading -- that is Binance's own
      answer for the month, and the REST path would have returned the same
      shorter frame;
    * when it was NOT published, the archive looks at the daily files of
      the leading period.  A leading day that is OLDER than the publication
      lag would have a file if the symbol had traded, so its absence PROVES
      the symbol did not trade then, and the shorter frame is correct;
    * when some leading day is still INSIDE the publication lag, its
      absence proves NOTHING.  The frame would silently start later than
      the window, so the load FAILS with :class:`BulkArchiveCoverageError`
      instead of serving it.

    THE CALLER CAN SEE IT.  The shortfall is recorded on the frame's
    ``attrs`` under the key ``COVERAGE_GAP_ATTR`` ("bulk_archive_coverage_gap"),
    as a dict with ``requested_start``, ``first_bar``, ``gap_days``,
    ``leading_month``, ``leading_month_published``, ``proven`` and
    ``reason``.  The ``attrs`` do not change the frame's shape, its columns
    or its values, so every existing caller keeps using the frame exactly
    as it does today.  Read it with ``bulk_archive.coverage_gap(frame)``,
    which returns ``None`` when there is no shortfall.

    Returns a DataFrame shaped exactly like the REST path's output, or an
    empty DataFrame when Binance has no archive for the window at all.
    """
    # The budget is SCALED to the window: a fixed budget cannot serve a
    # fixed-size window, and a two-year window needs 24 monthly archives
    # plus up to 31 daily files.  It is also bought up as the load goes
    # (see ``_extend_deadline``) and given up at once when the archive host
    # stops answering (see ``_collapse_deadline``).
    start_utc = _as_utc(start_dt)
    end_utc = _as_utc(end_dt)
    months = len(list(_iter_months(start_utc, end_utc))) if start_utc <= end_utc else 0
    began = time.monotonic()
    deadline = began + _archive_budget_seconds(months)
    token = _DEADLINE.set(deadline)
    start_token = _LOAD_START.set(began)
    collapsed_token = _COLLAPSED.set(False)
    made_token = _REQUESTS_MADE.set(0)
    expected_token = _EXPECTED_REQUESTS.set(_expected_request_count(months))
    failures_token = _FAILED_SINCE_SUCCESS.set([])
    unfetchable_token = _UNFETCHABLE_FILES.set([])
    try:
        return _fetch_klines_from_bulk_archive(
            symbol, timeframe, start_dt, end_dt, market_type
        )
    finally:
        _DEADLINE.reset(token)
        _LOAD_START.reset(start_token)
        _COLLAPSED.reset(collapsed_token)
        _REQUESTS_MADE.reset(made_token)
        _EXPECTED_REQUESTS.reset(expected_token)
        _FAILED_SINCE_SUCCESS.reset(failures_token)
        _UNFETCHABLE_FILES.reset(unfetchable_token)


def _fetch_klines_from_bulk_archive(
    symbol: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    market_type: str,
) -> Optional[pd.DataFrame]:
    """The body of ``fetch_klines_from_bulk_archive``, under its deadline."""
    start_dt = _as_utc(start_dt)
    end_dt = _as_utc(end_dt)
    if start_dt > end_dt:
        return _empty_result()

    interval = _normalise_interval(timeframe)
    now = datetime.now(timezone.utc)
    frames: List[pd.DataFrame] = []
    absent_days: List[datetime] = []
    missing_month_buckets: List[Tuple[datetime, datetime]] = []
    months = 0
    leading_month: Optional[datetime] = None
    leading_month_published: Optional[bool] = None

    for month_start in _iter_months(start_dt, end_dt):
        next_month = _add_month(month_start)
        months += 1

        monthly = _load_monthly_archive(symbol, interval, market_type, month_start)
        if leading_month is None:
            # The month the window STARTS in decides whether the leading
            # edge of the frame can be trusted: Binance published that
            # month or it did not.
            leading_month = month_start
            leading_month_published = monthly.published
        if monthly.frame is not None:
            frames.append(monthly.frame)
            # A PUBLISHED monthly archive can be missing whole days of its
            # own month.  The daily path catches an absent daily FILE;
            # nothing would catch a day missing from INSIDE the monthly
            # archive, so the missing days are collected here and refused
            # the same way below.
            missing_month_buckets.extend(
                _missing_buckets_in_month(
                    monthly.frame, interval, month_start, next_month, start_dt, end_dt
                )
            )
            continue

        # The monthly archive is not published: fall back to the daily files
        # of the days the window touches.
        daily, month_absent = _load_daily_archives(
            symbol, interval, market_type, start_dt, end_dt, month_start, next_month
        )
        absent_days.extend(month_absent)
        frames.extend(daily)

    if not frames:
        logger.warning(
            f"Bulk archive: no klines published for {symbol} ({timeframe}) "
            f"between {start_dt} and {end_dt}."
        )
        return _empty_result()

    result = pd.concat(frames)
    result = result.sort_index()
    result = result[~result.index.duplicated(keep="first")]
    result = result[(result.index >= start_dt) & (result.index <= end_dt)]
    if result.empty:
        logger.warning(
            f"Bulk archive: no klines inside the requested window for {symbol} "
            f"({timeframe}) between {start_dt} and {end_dt}."
        )
        return _empty_result()

    # Judge the missing data against the bars that were actually loaded:
    # only a day the symbol demonstrably traded through can be a hole.
    # The buckets are the calendar days the DAILY path found absent and the
    # calendar buckets the MONTHLY path found empty.
    first_bar = result.index[0]
    last_bar = result.index[-1]
    missing_buckets: List[Tuple[datetime, datetime]] = [
        (day, day + timedelta(days=1)) for day in absent_days
    ]
    missing_buckets.extend(missing_month_buckets)
    holes = [
        bucket
        for bucket in missing_buckets
        if _is_missing_bucket_a_hole(bucket[0], bucket[1], first_bar, last_bar, now)
    ]
    if holes:
        hole_text = ", ".join(bucket[0].strftime("%Y-%m-%d") for bucket in holes[:5])
        if len(holes) > 5:
            hole_text += f", ... (+{len(holes) - 5} more)"
        # The data that EXISTS is worth more than no data, so the frame is
        # served -- but the gap is named in the log and RECORDED on the
        # frame, so the caller is told exactly what is missing instead of
        # being handed a window it cannot see the hole in.
        logger.error(
            f"Bulk archive is MISSING {len(holes)} bucket(s) inside the traded "
            f"range {first_bar.isoformat()}..{last_bar.isoformat()} for {symbol} "
            f"({timeframe}) ({hole_text}). The frame is served with the gap "
            "RECORDED on it."
        )

    # THE LEADING EDGE.  A frame that starts LATER than the window it was
    # asked for is only legitimate when the archive can PROVE that the
    # symbol did not trade in the missing period -- that is a listing, and
    # the REST path would have returned the same shorter frame.  When the
    # archive CANNOT prove it, the shortfall is UNPROVEN, and the load
    # fails: a caller that cannot tell is left trading a window it did not
    # ask for, and reporting the KPIs of that window as the requested ones
    # is a wrong number.
    shortfall = _leading_shortfall(
        start_dt,
        first_bar.to_pydatetime(),
        interval,
        absent_days,
        leading_month_published,
        leading_month,
        now,
    )
    if shortfall is not None and not shortfall["proven"]:
        raise BulkArchiveCoverageError(
            f"Bulk archive cannot prove it covers {symbol} ({timeframe}) from "
            f"{shortfall['requested_start']}: the frame starts at "
            f"{shortfall['first_bar']} and {shortfall['reason']}. Refusing to "
            "return a frame that is shorter than the requested window."
        )
    if shortfall is not None:
        logger.warning(
            f"Bulk archive leading shortfall: symbol={symbol} "
            f"timeframe={timeframe} "
            f"requested_start={shortfall['requested_start']} "
            f"first_bar={shortfall['first_bar']} gap_days={shortfall['gap_days']} "
            f"leading_month={shortfall['leading_month']} "
            f"proven={shortfall['proven']} -- {shortfall['reason']}. "
            f"The frame does not cover the requested start."
        )
        result.attrs[COVERAGE_GAP_ATTR] = shortfall

    # THE DIFFERENCE FROM THE REST PATH, measured and attached to the frame.
    # This is what makes the difference something a caller can be TOLD
    # about instead of something that only exists in a log line.
    result.attrs[DIFFERENCE_ATTR] = _difference_record(
        symbol,
        timeframe,
        market_type,
        interval,
        start_dt,
        end_dt,
        result,
        shortfall,
        holes,
    )

    trailing_absent = [day for day in absent_days if day > last_bar]
    logger.info(
        f"Bulk archive coverage: symbol={symbol} timeframe={timeframe} "
        f"months={months} days_absent={len(absent_days)} "
        f"month_buckets_missing={len(missing_month_buckets)} "
        f"holes={len(holes)} "
        f"trailing_days_absent={len(trailing_absent)} "
        f"first_bar={first_bar.isoformat()} last_bar={last_bar.isoformat()} "
        f"rows={len(result)} "
        f"window={start_dt.isoformat()}..{end_dt.isoformat()} "
        f"leading_shortfall={'yes' if shortfall is not None else 'no'}"
    )
    return result
