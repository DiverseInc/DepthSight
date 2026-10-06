# File: tests/test_main_app_ws_escalation.py
"""
The main_app_ws CRITICAL escalation must not go silent during a long outage.

Background
----------
While SYMBOL_SOURCE_MODE is "MAIN_APP" there is NO automatic fallback -- the
static symbol list is only read when the mode is literally "STATIC_LIST". So
while that feed is down, every DYNAMIC-mode strategy has no symbols to match
or subscribe to. That is a trading outage, not a warning, and the code says so
in a comment right above the escalation.

The escalation then did this:

    if consecutive_failures in (threshold, threshold * 3, threshold * 10):

Exact membership. With threshold 10 that fires at 10, 30 and 100 -- and then
never again. The counter only resets on a SUCCESSFUL connect, so the longer
the outage ran, the quieter the logs got.

Observed live on 2026-10-06 at consecutive_failures=50: still WARNING, with
the CRITICAL silent for the previous 20 failures. This test pins the corrected
behaviour, including the exact failure count that regressed.
"""

from bot_module.data_consumer import should_escalate_ws_failure as esc

T = 10  # MAIN_APP_WS_CRITICAL_AFTER_FAILURES default


def test_below_threshold_never_escalates():
    for n in range(1, T):
        assert esc(n, T) is False, "escalated at %d, below threshold %d" % (n, T)


def test_escalates_at_the_threshold():
    assert esc(T, T) is True


def test_escalates_at_thirty():
    """The old predicate fired here; keep it firing."""
    assert esc(T * 3, T) is True


def test_escalates_at_one_hundred():
    assert esc(T * 10, T) is True


def test_escalates_at_fifty():
    """THE REGRESSION. Old predicate: 50 is in (10, 30, 100)? No. Silent."""
    assert esc(50, T) is True, (
        "a 50-failure outage must still escalate -- this is the exact "
        "regression observed live on 2026-10-06"
    )


def test_never_goes_silent_past_one_hundred():
    """Past 100 the old predicate was silent forever. Every multiple must fire."""
    for n in range(100, 1001, 10):
        assert esc(n, T) is True, "went silent at %d consecutive failures" % n


def test_does_not_flood_between_escalations():
    """`>=` alone would fire on every failure. It must stay on a cadence."""
    for n in range(T + 1, T + 10):
        assert esc(n, T) is False, "flooded at %d" % n


def test_cadence_is_at_least_five_failures():
    """A tiny threshold must not turn every failure into a CRITICAL."""
    assert esc(5, 1) is True
    assert esc(6, 1) is False
    assert esc(9, 1) is False
    assert esc(10, 1) is True


def test_survives_a_broken_threshold_config():
    """A junk MAIN_APP_WS_CRITICAL_AFTER_FAILURES value must not raise here.

    This runs inside the reconnect loop; raising would kill the task that
    maintains the symbol source.
    """
    for bad in (0, -5, None, "abc"):
        assert isinstance(esc(20, bad), bool)


def test_zero_failures_is_not_an_escalation():
    assert esc(0, T) is False
