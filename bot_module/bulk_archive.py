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
# that runs up to "now" legitimately has no file yet.  A missing daily file is
# only a genuine HOLE once it is older than this lag.
_PUBLICATION_LAG = timedelta(days=2)

# The key under which the loader records a LEADING-EDGE COVERAGE GAP on the
# frame's ``attrs``.  The frame may cover a SHORTER period than the one that
# was asked for -- the archive holds only what Binance published -- so a
# caller that must not trade a shorter window than it requested needs to be
# able to SEE that it happened.  The value is a dict; see
# ``_leading_coverage_gap`` for its keys.
#
# ``DataFrame.attrs`` is used because it does not change the frame's shape,
# its columns or its values: the existing callers keep indexing the frame
# exactly as they do today.  The loader carries the key across its own
# operations explicitly (see ``data_loader.download_klines``), because
# ``pandas.concat`` DROPS the attrs when the frames it merges do not all
# carry the same ones.
COVERAGE_GAP_ATTR = "bulk_archive_coverage_gap"

# Binance's kline intervals are a COUNT and a UNIT.  The unit is CASE
# SENSITIVE: "m" is minutes and "M" is months, and the archive keeps that
# distinction.
_INTERVAL_UNITS = ("s", "m", "h", "d", "w", "M")


class BulkArchiveCoverageError(RuntimeError):
    """The archives cannot cover the requested window.

    Raised when a daily file for a day that should already be published is
    missing and no monthly archive can complete that month.  Returning a
    gapped frame would silently change the meaning of a backtest: the P&L
    would look valid while trading a hole.  A wrong number is worse than no
    number, so the load fails instead.
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

    This MUST mirror ``data_loader._make_api_request`` exactly, because the
    archive is a FALLBACK for that REST path: for one request the two must
    answer with the same venue.

    The REST path sends ``market_type == "spot"`` to the spot host and EVERY
    other market type -- including ``futures_coinm`` -- to the USDT-M
    futures host (``data_loader._make_api_request``).  So the archive maps
    exactly one value to spot and EVERY other value to futures/um.

    Two things must never happen here:

    * a market type that merely CONTAINS "spot" must not get spot candles,
      because the REST path would have queried the futures host;
    * a COIN-M market type must not get COIN-M candles, because the REST
      path never asked Binance for COIN-M candles.  Serving them would swap
      the venue under the caller and change the numbers it gets back.
    """
    mt = (market_type or "").strip()
    if mt == "spot":
        return "spot"
    return "futures/um"


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
    """
    response = http_session.get(url, timeout=_request_timeout())
    if response.status_code == 404:
        logger.debug(f"Bulk archive not published: {url}")
        return None
    response.raise_for_status()
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


def _interval_duration(interval: str) -> timedelta:
    """The nominal duration of ONE bar of ``interval``.

    A calendar month has no fixed length, so "1M" uses the SHORTEST
    month (28 days): that is the smallest duration a monthly bar can
    cover, and using it keeps the leading-edge rule conservative.
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


def _is_missing_day_a_hole(day: datetime, now: datetime) -> bool:
    """True when a missing daily file for ``day`` is a genuine HOLE.

    The trailing edge of a window that runs to "now" legitimately has no file
    yet, because Binance publishes the daily archives with about a day of
    lag.  A day that ended well before that lag window must have a file.
    """
    return (day + timedelta(days=1)) <= (now - _PUBLICATION_LAG)


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
    whatever phase Binance aligns its bars to: a spot daily bar opens at
    00:00 UTC+08:00, not at midnight UTC.  For a calendar-month interval
    the previous bar is the first of the previous month.
    """
    count, unit = _parse_interval(interval)
    if unit == "M":
        month_start = first_bar.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return _add_month(month_start, -count)
    return first_bar - _interval_duration(interval)


def _leading_coverage_gap(
    requested_start: datetime,
    first_bar: datetime,
    interval: str,
    leading_month_published: Optional[bool],
    leading_month: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    """The COVERAGE GAP at the leading edge of the window, or ``None``.

    A frame may cover a SHORTER period than the one that was asked for.
    That is normal and legitimate in one case: the symbol was listed
    later, and the archive holds only the history that exists.  It is
    DANGEROUS in another: the archives for the leading month were never
    published, so the archive cannot prove that the symbol simply did not
    trade then -- it might be that the data for that month is missing
    altogether, and the frame would silently start later than the window.

    The two are told apart by ``leading_month_published``:

    * the leading month's monthly archive WAS published -> Binance's own
      answer for that month, so the frame starts where the symbol started
      and there is NO gap;
    * it was NOT published -> the coverage of the leading edge is
      UNKNOWN, and the gap is reported.

    The gap itself is measured on the archive's own bar grid: the bar
    that would come immediately before ``first_bar`` decides it.  If that
    bar would fall inside the requested window, it should have been
    returned and was not -- that is the gap.  If it would fall OUTSIDE
    the window, the frame starts exactly where the REST path would have
    started it, and there is nothing to report.
    """
    if leading_month_published is not False:
        # The leading month was published: the archive holds Binance's own
        # answer for it, so a shorter history is a LISTING, not a gap.
        return None

    previous_bar = _previous_bar_open(first_bar, interval)
    if previous_bar < requested_start:
        # The bar before the first one falls outside the requested window,
        # so the frame starts exactly where the REST path starts it.
        return None

    gap = first_bar - requested_start
    return {
        "requested_start": requested_start.isoformat(),
        "first_bar": first_bar.isoformat(),
        "gap_days": gap.days,
        "leading_month": (
            leading_month.strftime("%Y-%m") if leading_month is not None else None
        ),
        "leading_month_published": False,
        "reason": (
            "the leading month's monthly archive is not published, so the "
            "archive cannot prove that the symbol was not listed yet; the "
            "frame starts later than the requested window"
        ),
    }


def coverage_gap(frame: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """The leading-edge coverage gap recorded on ``frame``, if there is one.

    ``None`` means "no leading-edge coverage gap was detected".  The
    returned dict carries the requested start, the first bar the archive
    could supply, the size of the gap in days and why it was reported.
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
    payload = fetch_archive_bytes(url)
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
            payload = fetch_archive_bytes(url)
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

    COVERAGE IS FAIL-CLOSED, BUT ONLY WHERE THERE IS A HOLE.  An absent
    daily file is a hole when the symbol demonstrably existed on that day:
    the day falls after the first bar observed for this window and before
    the last one, and it is older than the publication lag.  Then
    :class:`BulkArchiveCoverageError` is raised rather than a gapped frame
    being returned -- a backtest that silently trades a hole produces a P&L
    that looks valid and is not, and a wrong number is worse than no number.

    A PUBLISHED MONTHLY ARCHIVE IS HELD TO THE SAME RULE.  Binance can
    publish a monthly archive that is missing whole days of its own month,
    and no absent daily file would ever report that: the monthly archive is
    used as it is.  A calendar bucket the window touches that holds no bar at
    all is therefore a hole too, and the load fails the same way.

    A day before the first observed bar is NOT a hole: the symbol was not
    listed yet, and the window simply carries the shorter history the
    archive holds.  A day after the last observed bar is not a hole either
    (delisted, or the trailing publication lag).

    THE WHOLE FALLBACK RUNS UNDER A DEADLINE.  One archive request can burn
    a whole request timeout and the trailing month of a window can need 31
    daily files, so an unreachable archive host would stall the loader for
    MINUTES.  The load therefore gets a fixed budget
    (``config.BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS``); when it is spent the
    load fails loudly with :class:`BulkArchiveDeadlineExceeded` and NOTHING
    is served -- never the candles collected before the deadline.

    KNOWN DIFFERENCES FROM THE REST PATH, both inherent to the source and
    reported in the log line below: the archive holds only CLOSED bars (REST
    also returns the still-forming one), and the trailing edge of a window
    that runs to "now" is short by the publication lag.

    THE FRAME MAY COVER A SHORTER PERIOD THAN THE ONE REQUESTED.  The
    archive holds what Binance published, and Binance publishes nothing
    for a symbol that did not trade, so a window that starts before the
    symbol was listed comes back starting later.  That is normal, and the
    load does not fail for it -- but it must never happen UNSEEN, because a
    backtest asked for 2024-01-01..2025-12-31 that silently receives a
    frame starting 2024-02-01 reports the KPIs of the shorter window as if
    they were the requested ones.

    So the loader MEASURES the leading edge and REPORTS it:

    * when the leading month's monthly archive WAS published, the frame
      starts wherever the symbol started trading and there is no gap --
      nothing is reported;
    * when it was NOT published, the coverage of the leading edge is
      UNKNOWN (the archives for that month cannot prove that the symbol
      did not trade then), and the gap is reported at WARNING level with
      the numbers, and recorded on the frame.

    THE CALLER CAN SEE IT.  The gap is recorded on the frame's ``attrs``
    under the key ``COVERAGE_GAP_ATTR`` ("bulk_archive_coverage_gap"), as
    a dict with ``requested_start``, ``first_bar``, ``gap_days``,
    ``leading_month``, ``leading_month_published`` and ``reason``.  The
    ``attrs`` do not change the frame's shape, its columns or its values,
    so every existing caller keeps using the frame exactly as it does
    today.  Read it with ``bulk_archive.coverage_gap(frame)``, which
    returns ``None`` when there is no gap.

    Returns a DataFrame shaped exactly like the REST path's output, or an
    empty DataFrame when Binance has no archive for the window at all.
    """
    deadline = time.monotonic() + float(config.BULK_ARCHIVE_TOTAL_TIMEOUT_SECONDS)
    token = _DEADLINE.set(deadline)
    try:
        return _fetch_klines_from_bulk_archive(
            symbol, timeframe, start_dt, end_dt, market_type
        )
    finally:
        _DEADLINE.reset(token)


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
        raise BulkArchiveCoverageError(
            f"Bulk archive cannot cover {symbol} ({timeframe}): "
            f"{len(holes)} bucket(s) inside the traded range "
            f"{first_bar.isoformat()}..{last_bar.isoformat()} have no published "
            f"archive ({hole_text}). Refusing to return a gapped frame."
        )

    # THE LEADING EDGE.  A hole inside the traded range fails the load, but a
    # window that simply starts LATER than it was asked to cannot: the
    # symbol may simply have been listed later, and that is not a fault.
    # What must never happen is that it happens UNSEEN, so the gap is
    # measured here, reported at WARNING level with the numbers, and
    # recorded on the frame so a caller can refuse to trade it.
    gap = _leading_coverage_gap(
        start_dt,
        first_bar.to_pydatetime(),
        interval,
        leading_month_published,
        leading_month,
    )
    if gap is not None:
        logger.warning(
            f"Bulk archive coverage gap: symbol={symbol} timeframe={timeframe} "
            f"requested_start={gap['requested_start']} "
            f"first_bar={gap['first_bar']} gap_days={gap['gap_days']} "
            f"leading_month={gap['leading_month']} "
            f"leading_month_published=False -- {gap['reason']}. "
            f"The frame does not cover the requested start."
        )
        result.attrs[COVERAGE_GAP_ATTR] = gap

    trailing_absent = [day for day in absent_days if day > last_bar]
    logger.info(
        f"Bulk archive coverage: symbol={symbol} timeframe={timeframe} "
        f"months={months} days_absent={len(absent_days)} "
        f"month_buckets_missing={len(missing_month_buckets)} "
        f"trailing_days_absent={len(trailing_absent)} "
        f"first_bar={first_bar.isoformat()} last_bar={last_bar.isoformat()} "
        f"rows={len(result)} "
        f"window={start_dt.isoformat()}..{end_dt.isoformat()} "
        f"leading_coverage_gap={'yes' if gap is not None else 'no'}"
    )
    return result
