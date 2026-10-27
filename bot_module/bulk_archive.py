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

import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

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


class BulkArchiveCoverageError(RuntimeError):
    """The archives cannot cover the requested window.

    Raised when a daily file for a day that should already be published is
    missing and no monthly archive can complete that month.  Returning a
    gapped frame would silently change the meaning of a backtest: the P&L
    would look valid while trading a hole.  A wrong number is worse than no
    number, so the load fails instead.
    """

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

    The comparison is EXACTLY the one the REST path uses to choose its host
    (``market_type == "spot"`` in ``data_loader._make_api_request``).  A
    substring test would be a silent venue swap: a market type that merely
    CONTAINS "spot" would get spot candles from the archive while the REST
    path had queried the futures host.
    """
    mt = (market_type or "").strip()
    if mt == "spot":
        return "spot"
    if mt in ("futures_coinm", "futures_cm", "coinm"):
        return "futures/cm"
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


def _daily_url(
    symbol: str, interval: str, market_type: str, day: datetime
) -> str:
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
    """
    response = http_session.get(url, timeout=60)
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
    frame[RESULT_COLUMNS] = frame[RESULT_COLUMNS].apply(
        pd.to_numeric, errors="coerce"
    )
    frame = frame[~frame.index.duplicated(keep="first")]
    return frame[RESULT_COLUMNS]


def parse_archive_candles(zip_bytes: bytes) -> pd.DataFrame:
    """Turns one archive ``.zip`` into the loader's klines DataFrame."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            members = [
                name for name in archive.namelist() if not name.endswith("/")
            ]
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


def _add_month(month_start: datetime) -> datetime:
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


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
    """
    if not _is_missing_day_a_hole(day, now):
        return False
    if day + timedelta(days=1) <= first_bar:
        # Entirely before the first observed bar: not listed yet.
        return False
    if day > last_bar:
        # Entirely after the last observed bar: delisted / trailing edge.
        return False
    return True


def _load_monthly_archive(
    symbol: str, interval: str, market_type: str, month_start: datetime
) -> Optional[pd.DataFrame]:
    """Fetches one monthly archive.  ``None`` when it is not published."""
    url = _monthly_url(symbol, interval, market_type, month_start)
    payload = fetch_archive_bytes(url)
    if payload is None:
        return None
    frame = parse_archive_candles(payload)
    if frame.empty:
        logger.warning(f"Bulk archive: monthly archive is empty {url}")
        return None
    logger.info(f"Bulk archive: loaded monthly archive {url}")
    return frame


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

    A day before the first observed bar is NOT a hole: the symbol was not
    listed yet, and the window simply carries the shorter history the
    archive holds.  A day after the last observed bar is not a hole either
    (delisted, or the trailing publication lag).

    KNOWN DIFFERENCES FROM THE REST PATH, both inherent to the source and
    reported in the log line below: the archive holds only CLOSED bars (REST
    also returns the still-forming one), and the trailing edge of a window
    that runs to "now" is short by the publication lag.

    Returns a DataFrame shaped exactly like the REST path's output, or an
    empty DataFrame when Binance has no archive for the window at all.
    """
    start_dt = _as_utc(start_dt)
    end_dt = _as_utc(end_dt)
    if start_dt > end_dt:
        return _empty_result()

    interval = _normalise_interval(timeframe)
    now = datetime.now(timezone.utc)
    frames: List[pd.DataFrame] = []
    absent_days: List[datetime] = []
    months = 0

    for month_start in _iter_months(start_dt, end_dt):
        next_month = _add_month(month_start)
        months += 1

        monthly = _load_monthly_archive(symbol, interval, market_type, month_start)
        if monthly is not None:
            frames.append(monthly)
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

    # Judge the absent days against the bars that were actually loaded: only
    # a day the symbol demonstrably traded through can be a hole.
    first_bar = result.index[0]
    last_bar = result.index[-1]
    holes = [
        day
        for day in absent_days
        if _is_absent_day_a_hole(day, first_bar, last_bar, now)
    ]
    if holes:
        hole_text = ", ".join(day.strftime("%Y-%m-%d") for day in holes[:5])
        if len(holes) > 5:
            hole_text += f", ... (+{len(holes) - 5} more)"
        raise BulkArchiveCoverageError(
            f"Bulk archive cannot cover {symbol} ({timeframe}): "
            f"{len(holes)} day(s) inside the traded range "
            f"{first_bar.isoformat()}..{last_bar.isoformat()} have no published "
            f"archive ({hole_text}). Refusing to return a gapped frame."
        )

    trailing_absent = [day for day in absent_days if day > last_bar]
    logger.info(
        f"Bulk archive coverage: symbol={symbol} timeframe={timeframe} "
        f"months={months} days_absent={len(absent_days)} "
        f"trailing_days_absent={len(trailing_absent)} "
        f"first_bar={first_bar.isoformat()} last_bar={last_bar.isoformat()} "
        f"rows={len(result)} "
        f"window={start_dt.isoformat()}..{end_dt.isoformat()}"
    )
    return result
