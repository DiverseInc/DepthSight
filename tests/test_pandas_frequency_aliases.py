# tests/test_pandas_frequency_aliases.py
"""
Guard against pandas 3 removing the deprecated single-letter frequency aliases.

Background, 2026-10-14. This repo's first-ever CI run produced 132 failures, and
~121 of them collapsed to one root cause: pandas 3.0 removed the deprecated
single-letter frequency aliases (T, H, S, L, U, N, ...). The failures presented
as 121 independent bugs. They were nine strings.

One of them was in PRODUCTION, not tests:

    bot_module/genetic_strategy_finder.py   {"5m": "5T", "15m": "15T", ...}

It had been dead since pandas 3 landed -- silently, because the genetic search
is an offline tool nobody watches. Same shape as the ma_cross direction bug: a
path that quietly produces a wrong answer forever instead of raising.

## Why this test is context-aware, not a denylist

The naive fix -- "replace M with ME everywhere" -- is WRONG, and measurably so.
pandas 3.0.6 treats the two APIs as opposites:

    to_period("M")   -> OK
    to_period("ME")  -> ValueError: for Period, please use 'M' instead of 'ME'
    resample("ME")   -> OK
    resample("M")    -> ValueError: Invalid frequency: M

So a blanket find-and-replace would have BROKEN working code. A reviewer
recommended exactly that, and the test suite caught it. Hence two vocabularies:

  PERIOD context (to_period)  -> "M" is correct,  "ME" is invalid
  TIMED  context (resample, Grouper(freq=), date_range(freq=), asfreq)
                              -> "ME" is correct, "M" is invalid

T/H/S/L/U/N are invalid in BOTH and are replaced everywhere.

Every classification below is measured against the installed pandas, not
guessed. Do not "simplify" it back into a flat denylist.
"""

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# (pattern, context). Context decides which vocabulary applies.
PERIOD = "period"
TIMED = "timed"

FREQUENCY_ARGUMENT_PATTERNS = [
    (re.compile(r"\.to_period\(\s*['\"]([^'\"]+)['\"]"), PERIOD),
    (re.compile(r"\.resample\(\s*['\"]([^'\"]+)['\"]"), TIMED),
    (re.compile(r"\.asfreq\(\s*['\"]([^'\"]+)['\"]"), TIMED),
    (re.compile(r"Grouper\(\s*freq\s*=\s*['\"]([^'\"]+)['\"]"), TIMED),
    (re.compile(r"date_range\([^)]*?freq\s*=\s*['\"]([^'\"]+)['\"]"), TIMED),
]

# Removed in pandas 3 and invalid in BOTH contexts.
REMOVED_IN_BOTH = {"T", "H", "S", "L", "U", "N", "A", "Y", "Q", "BM", "BY", "BQ", "BA"}

# Removed, but only in the timed context -- in Period context "M" is still valid.
REMOVED_IN_TIMED_ONLY = {"M"}

# Never deprecated. Start-of-period variants stay legal in the timed context.
LEGAL_IN_TIMED = {"ME", "MS", "YE", "YS", "QE", "QS", "BME", "BYE", "BQE", "BAE"}

# Compound offsets: "5T", "1H" end in a removed letter. Case-sensitive, so a
# valid "1h" is never matched.
COMPOUND_REMOVED_SUFFIX = re.compile(r"^[0-9]*([THSULN])$")


def _is_removed(freq: str, context: str) -> bool:
    """Is `freq` rejected by pandas 3 in this context? Measured, not guessed."""
    if context == PERIOD:
        # "M" is still correct for Period; "ME" is what pandas rejects here.
        if freq == "ME":
            return True
        if freq in REMOVED_IN_BOTH:
            return True
        return COMPOUND_REMOVED_SUFFIX.match(freq) is not None
    # TIMED
    if freq in REMOVED_IN_BOTH or freq in REMOVED_IN_TIMED_ONLY:
        return True
    if freq in LEGAL_IN_TIMED:
        return False
    return COMPOUND_REMOVED_SUFFIX.match(freq) is not None


def _iter_source_files():
    """Scan production AND tests -- the regression came from both."""
    for subdir in ("bot_module", "api", "tests"):
        root = PROJECT_ROOT / subdir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            # Never scan this file: it necessarily contains the bad tokens.
            if path.resolve() == Path(__file__).resolve():
                continue
            yield path


def _offenders(path: Path):
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, context in FREQUENCY_ARGUMENT_PATTERNS:
            for match in pattern.finditer(line):
                freq = match.group(1)
                if _is_removed(freq, context):
                    hits.append((lineno, freq, context, stripped))
                    break
            else:
                continue
            break
    return hits


def test_no_removed_pandas_frequency_aliases_in_source():
    """Fail loudly, naming every file:line, so the offender takes seconds to fix."""
    problems = []
    for path in _iter_source_files():
        for lineno, freq, context, text in _offenders(path):
            rel = path.relative_to(PROJECT_ROOT)
            problems.append(f"  {rel}:{lineno}  {context}: {freq!r} -> {text}")

    assert not problems, (
        "pandas 3.0 rejects these frequency aliases.\n"
        "  timed  (resample/Grouper/date_range/asfreq): M->ME, T->min, H->h, "
        "S->s, L->ms, U->us, N->ns\n"
        "  period (to_period): M stays 'M'; 'ME' is what pandas REJECTS there\n"
        "NOTE: a bare single-letter grep will NOT work -- Binance WebSocket payload "
        'keys ("S", "M", "B", "X", "L", "N", "E") appear throughout api/ and '
        "bot_module/exchanges/ and would swamp the signal.\n"
        + "\n".join(problems)
    )


def test_guard_detects_removed_aliases_and_spares_valid_ones():
    """A guard that cannot fail is worse than no guard -- prove it bites."""
    tmp = PROJECT_ROOT / "tests" / "_tmp_alias_probe.py"
    try:
        tmp.write_text(
            'df.resample("5T")\n'
            'df.resample("15T")\n'
            'df.resample("1H")\n'
            'df.resample("M")\n'
            'df.to_period("ME")\n'
            'df.to_period("M")\n'
            'df.resample("ME")\n'
            'df.resample("MS")\n'
            'df.resample("1min")\n'
            'df.resample("5min")\n'
            'df.resample("1h")\n',
            encoding="utf-8",
        )
        found = {(freq, context) for _l, freq, context, _t in _offenders(tmp)}
    finally:
        tmp.unlink(missing_ok=True)

    # Must flag: the removed ones, plus the two context-specific inversions.
    assert ("5T", TIMED) in found, f"missed 5T; found {sorted(found)}"
    assert ("15T", TIMED) in found, f"missed 15T; found {sorted(found)}"
    assert ("1H", TIMED) in found, f"missed 1H; found {sorted(found)}"
    assert ("M", TIMED) in found, f"missed resample('M'); found {sorted(found)}"
    assert ("ME", PERIOD) in found, f"missed to_period('ME'); found {sorted(found)}"

    # Must NOT flag valid code.
    for freq, context in (("M", PERIOD), ("ME", TIMED), ("MS", TIMED),
                          ("1min", TIMED), ("5min", TIMED), ("1h", TIMED)):
        assert (freq, context) not in found, (
            f"false positive: {context} {freq!r} is valid in pandas 3.0.6"
        )


def test_classification_matches_the_installed_pandas():
    """Tie every rule to the real library, so the table cannot rot."""
    pd = pytest.importorskip("pandas")
    series = pd.Series(
        pd.date_range("2024-01-01", periods=48, freq="1h")
    )
    frame = pd.DataFrame({"v": range(48)}, index=series.index)

    def works(callable_):
        try:
            callable_()
            return True
        except Exception:
            return False

    for freq in ("5T", "15T", "1H", "4H", "M", "S"):
        assert _is_removed(freq, TIMED) == (not works(lambda f=freq: frame.resample(f).sum())), (
            f"guard disagrees with pandas for resample({freq!r})"
        )
    for freq in ("M", "5T", "1H"):
        assert _is_removed(freq, PERIOD) == (not works(lambda f=freq: series.dt.to_period(f))), (
            f"guard disagrees with pandas for to_period({freq!r})"
        )


import pytest  # noqa: E402  (imported late so the module docstring reads first)