"""The unknown-scope latch must be RELEASABLE by the sandbox that raised it.

THE DEFECT
----------
``_mark_guard_state_scope_unknown()`` sets ONE latch, and that latch has ONE
release site: the LIVE rollover, in ``_check_and_reset_daily_stats()``::

    if current_day_str_utc > self._guard_state_scope_unknown_day_str:
        self._guard_state_scope_unknown = False

A keyless PAPER account (``bot_runner.py:563`` passes ``executor=None,
paper_executor=paper_executor``) has no live account behind it, so
``initialize_balance()`` returns before the live start-up machinery and EVERY
rollover it ever runs is the PAPER one. It can therefore never reach the
release site.

The consequence was not a pause, it was a BRICK:

* the PAPER gate stayed closed for the life of the process;
* ``save_state()`` refuses to write while the scope is unknown, so the
  unreadable payload survived every restart;
* the next start-up read the same unreadable payload, latched the same latch,
  and could not release it either.

So one unreadable day stamp on a PAPER account disabled that account
permanently, with no day rollover and no restart able to heal it. The
docstring of ``_mark_guard_state_scope_unknown`` promises the opposite --
"the latch is released at the UTC day rollover".

WHAT IS PINNED HERE
-------------------
1. A keyless PAPER account whose PAPER scope became unreadable is RELEASED at
   the PAPER day rollover, on a STRICTLY LATER day.
2. The same day does NOT release it: a fresh process sees its first day-key
   change on the very same start that set the latch, so the strictness of the
   rule is the whole point.
3. An account that HAS a live sandbox is NOT released by the PAPER rollover.
   The PAPER rollover does not restart the LIVE day counters, so it must not
   be able to open a LIVE gate; that account keeps the single release site it
   has always had.

These tests drive the REAL ``RiskManager`` and the REAL ``restore_state()``
against a fake Redis, in the start-up order the controller uses (restore,
then initialize).
"""

import logging

import pytest

from tests.test_per_mode_risk_gating import (
    FakeRedis,
    PAPER_BALANCE,
    TODAY,
    _book_closed_trade,
    _dual_mode_risk_manager,
    _guard_key,
    _paper_only_risk_manager,
)
from tests.test_risk_restore_missing_field_fails_closed import _write_payload

# The day after TODAY. The release is on a STRICTLY LATER day only.
TOMORROW = "2026-10-28"

# The PAPER account's day: a $500 loss against a $5,000 opening balance is a
# 10% loss, which breaches the 5% daily-loss limit in the PAPER sandbox.
PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT = -500.0


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_a_keyless_paper_account(redis_store):
    """A keyless PAPER account whose own daily loss closed its gate.

    The payload in Redis is produced by the product's own ``save_state()``,
    so it is exactly what a real restart would find.
    """
    rm = _paper_only_risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    await _book_closed_trade(
        rm, "BTCUSDT", PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT, mode="paper"
    )
    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate must be CLOSED before the restart, or the tests below "
        "would pass for the wrong reason"
    )
    return rm


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# 1. THE KEYLESS PAPER ACCOUNT IS RELEASED AT THE PAPER ROLLOVER
# ---------------------------------------------------------------------------


async def test_a_keyless_paper_account_is_released_at_the_paper_rollover(
    redis_store, caplog
):
    """The PAPER rollover must be able to release a PAPER-scoped latch.

    Before the fix this account could never trade again: the latch's only
    release site was the LIVE rollover, which a keyless PAPER account never
    runs, and ``save_state`` refused to write while the scope was unknown, so
    the unreadable payload survived every restart.
    """
    rm = await _seed_a_keyless_paper_account(redis_store)
    _write_payload(redis_store, rm, drop_paper=("day_str",))

    rm_b = _paper_only_risk_manager(FakeRedis(redis_store))
    await rm_b.restore_state()

    assert rm_b._guard_state_scope_unknown is True, (
        "the unreadable PAPER day stamp did not latch the unknown-scope state"
    )
    assert rm_b._paper_is_trading_allowed is False, (
        "the PAPER gate must be CLOSED while the scope is unknown"
    )

    # The SAME day must not release it.
    rm_b._check_and_reset_paper_daily_stats()
    assert rm_b._guard_state_scope_unknown is True, (
        "the latch was released on the SAME UTC day the scope became "
        "unreadable: a fresh process sees its first day-key change on the "
        "very same start that set the latch"
    )

    # The NEXT day does.
    rm_b._fake_day = TOMORROW
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.update_balance(mode="paper")

    assert rm_b._guard_state_scope_unknown is False, (
        "the keyless PAPER account was never released: the latch's only "
        "release site was the LIVE rollover, which this account never runs, "
        "and the unreadable payload survived every restart"
    )
    assert rm_b._paper_is_trading_allowed is True, (
        "the PAPER gate stayed closed after the day rolled over: the new UTC "
        "day starts the day's PnL and the loss streak at zero by definition, "
        "so the account is no longer trading on unknown numbers"
    )
    assert any(
        "Paper trading is released for the new day" in m for m in _messages(caplog)
    ), f"the release was not reported at CRITICAL, got {_messages(caplog)}"


# ---------------------------------------------------------------------------
# 2. THE PAPER ROLLOVER MUST NOT RELEASE AN ACCOUNT WITH A LIVE SANDBOX
# ---------------------------------------------------------------------------


async def test_the_paper_rollover_does_not_release_an_account_with_a_live_sandbox(
    redis_store,
):
    """The PAPER rollover must not be a second way to open a LIVE gate.

    The PAPER rollover restarts the PAPER day counters. It does not restart
    the LIVE ones, so it must not be able to release a latch that stands for
    an unknown LIVE scope -- and an account that has a live executor keeps the
    single release site it has always had.
    """
    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    await rm.update_balance(mode="paper")
    await _book_closed_trade(
        rm, "BTCUSDT", PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT, mode="paper"
    )
    _write_payload(redis_store, rm, drop_paper=("day_str",))

    rm_b = _dual_mode_risk_manager(FakeRedis(redis_store))
    await rm_b.restore_state()
    assert rm_b._guard_state_scope_unknown is True

    rm_b._fake_day = TOMORROW
    await rm_b.update_balance(mode="paper")

    assert rm_b._guard_state_scope_unknown is True, (
        "the PAPER rollover released the unknown-scope latch for an account "
        "that HAS a live sandbox: the PAPER rollover does not restart the "
        "LIVE day counters, so it must not be able to open a LIVE gate"
    )
    assert rm_b._paper_is_trading_allowed is False
