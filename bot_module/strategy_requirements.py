"""
Per-strategy candle-history requirements (2026-10-10).

WHY THIS EXISTS
---------------
`MIN_STRATEGY_HISTORY_CANDLES` is a single global constant applied to every
`kline_<tf>` key of every strategy:

    controller.py  -- the READ gate: refuses to evaluate below N candles
    data_consumer  -- the DOWNLOAD: asks for enough days to satisfy N

That worked only because N was raised to 205 to accommodate one strategy's
200-period average. It is a coincidence, not a design: a strategy built in
the visual editor with a 300-period EMA is **permanently unsatisfiable**,
because the gate demands 300 and the download only ever asks for 205. The
strategy reports "running" and silently never trades -- the exact failure
class this codebase keeps producing.

The requirement should come from the strategy's own configured indicator
periods, which is what upstream DepthSight calls `min_candles`.

ONE RULE GOVERNS BOTH SIDES
---------------------------
The READ gate and the DOWNLOAD must agree. Raising only the gate converts a
false-negative into a permanently-unsatisfiable requirement. Both call
`required_candles_for_indicator_keys()` below, so they cannot drift.

DERIVATION IS DELIBERATELY GENEROUS
-----------------------------------
From `EMA_200` we must require 200 candles; `BB_20_2.0` requires 20 (not the
2.0 std-dev). Taking the MAXIMUM integer token over-estimates in ambiguous
cases (`MACD_12_26_9` -> 26, the slow period, which is correct).

That direction is chosen on purpose: **over-requiring costs one extra fetch,
under-requuing produces a strategy that never fires and says nothing.** When
in doubt this module errs high.

Not derived (falls back to the global floor):
  - `tape_*` -- window sizes are SECONDS, not candles; mixing them in would
    demand absurd history.
  - Non-numeric suffixes.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: Extra candles required beyond the largest indicator period. Covers the
#: previous value needed for a cross (`current_candle_index - 1`) plus
#: indicator warmup that is not a simple period.
HISTORY_BUFFER_CANDLES = 10

_TOKEN_RE = re.compile(r"\d+")


def max_period_for_indicator_keys(keys: Optional[Iterable[str]]) -> int:
    """Largest candle period implied by indicator keys such as ``EMA_200``.

    Returns 0 when nothing parseable is present, which callers should treat
    as "no opinion, use the global floor".
    """
    best = 0
    for key in keys or ():
        if not isinstance(key, str) or not key:
            continue
        # tape_* windows are seconds, not candles.
        if key.startswith("tape_"):
            continue
        for match in _TOKEN_RE.finditer(key):
            try:
                value = int(match.group())
            except ValueError:  # pragma: no cover - regex is digits-only
                continue
            if value > best:
                best = value
    return best


def required_candles_for_indicator_keys(
    keys: Optional[Iterable[str]],
    *,
    floor: int = 0,
    buffer: int = HISTORY_BUFFER_CANDLES,
) -> int:
    """Candles a strategy needs, never below ``floor``.

    ``floor`` is ``MIN_STRATEGY_HISTORY_CANDLES``. It is a floor and not a
    replacement: a strategy using only RSI(14) still gets the configured
    minimum, and one using EMA(200) gets 200 + buffer.
    """
    derived = max_period_for_indicator_keys(keys)
    if derived <= 0:
        return max(floor, 0)
    return max(floor, derived + buffer)