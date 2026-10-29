"""A persisted guard state that is MISSING a field must not restore as a ZERO.

THE DEFECT
----------
``_apply_persisted_guard_state`` read the day's PnL and the loss streak with
the idiom ``payload.get(field) or 0``. That idiom cannot tell THREE different
things apart from a genuine zero:

* the field is MISSING from the payload -> ``payload.get`` returns ``None``;
* the field is present but NULL       -> ``payload.get`` returns ``None``;
* the field is present and is a zero   -> ``payload.get`` returns ``0``.

``None or 0`` is ``0``, so a partial or corrupt payload restored as "no loss
today" and "no loss streak". On the LIVE path that assigned
``self.stats.today_pnl = 0.0`` outright: zero PnL means "no loss today", so
the daily-loss gate -- a REAL-MONEY control -- stopped measuring, and a
previously blocked account was free to trade again. On the PAPER path the
same shape assigned ``self.paper_stats.today_pnl = 0.0`` and then re-checked
the gate on the zeroed numbers, so the PAPER gate re-opened the same way.

The PAPER path already said, in its own comment, that "A PERSISTED VALUE
THAT CANNOT BE READ IS NOT A ZERO" -- but it only ever reached that branch
for a value that EXISTS and cannot be cast. A missing or null field never
got there: it became a zero first.

WHAT IS PINNED HERE
-------------------
1. A LIVE payload MISSING ``today_pnl`` does not zero the in-memory day's
   PnL, and does not re-open the daily-loss gate.
2. A LIVE payload with ``today_pnl: null`` does the same.
3. A LIVE payload MISSING ``consecutive_losses`` does not lower the in-memory
   streak, and is reported as UNREADABLE rather than silently read as a zero.
4. The COUNTERWEIGHT: a payload with a genuinely READABLE ``today_pnl: 0.0``
   and ``consecutive_losses: 0`` still restores as zero. "Absent" and "zero"
   are DIFFERENT facts -- one of them is an unknown, the other is a
   measurement -- and a fix that cannot tell them apart has swapped one
   fail-open for another.
5. The PAPER path behaves IDENTICALLY to the LIVE path for the same
   malformed payload. That parity is the point of the change: the two
   sandboxes must not disagree about what an unreadable persisted value
   means.

These tests drive the REAL ``RiskManager`` and the REAL ``restore_state()``
against a fake Redis. The end state asserted is the product's, not a stub's.
"""

import json
import logging

import pytest

from tests.test_per_mode_risk_gating import (
    FakeRedis,
    LIVE_BALANCE,
    PAPER_BALANCE,
    _book_closed_trade,
    _dual_mode_risk_manager,
    _guard_key,
)


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


# The LIVE account's day: a $600 loss against a $10,000 opening balance is a
# 6% loss, which BREACHES the 5% daily-loss limit -- so the LIVE gate is
# CLOSED before the restore. That is what makes the restore observable: a
# restore that reads the day's PnL as zero measures no loss at all and
# RE-OPENS the gate.
LIVE_LOSS_THAT_BREACHES_THE_DAILY_LIMIT = -600.0

# The PAPER account's day: a $500 loss against a $5,000 opening balance is a
# 10% loss, which breaches the same 5% limit in the PAPER sandbox.
PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT = -500.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_payload(
    redis_store,
    rm,
    *,
    drop_live=(),
    null_live=(),
    live_overrides=None,
    drop_paper=(),
    null_paper=(),
    paper_overrides=None,
):
    """Overwrite the persisted payload with a malformed one.

    This is how a PARTIAL or CORRUPT write is simulated: the value that
    reaches Redis is missing, null, or simply not there -- so the restore has
    to decide what an unreadable persisted value means.

    `drop_*` names fields to REMOVE and `null_*` names fields to set to null;
    both are shapes a truncated or half-written payload can leave behind.
    """
    key = _guard_key(rm)
    payload = json.loads(redis_store[key])
    for name in drop_live:
        payload.pop(name, None)
    for name in null_live:
        payload[name] = None
    if live_overrides:
        payload.update(live_overrides)
    paper = payload.get("paper")
    if isinstance(paper, dict):
        for name in drop_paper:
            paper.pop(name, None)
        for name in null_paper:
            paper[name] = None
        if paper_overrides:
            paper.update(paper_overrides)
    redis_store[key] = json.dumps(payload)
    return payload


async def _live_account_blocked_by_its_daily_loss(redis_store):
    """A LIVE account whose day already breaches the daily-loss limit.

    The account is started for real (``initialize_balance``) and the loss is
    booked the way the controller books it, so the in-memory state is the
    product's own state and not something a test made up.
    """
    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    await _book_closed_trade(
        rm, "BTCUSDT", LIVE_LOSS_THAT_BREACHES_THE_DAILY_LIMIT, mode="live"
    )
    assert rm.stats.current_balance == pytest.approx(LIVE_BALANCE)
    assert rm.stats.today_pnl == pytest.approx(LIVE_LOSS_THAT_BREACHES_THE_DAILY_LIMIT)
    assert rm._is_trading_allowed is False, (
        "the LIVE gate must be CLOSED before the restore, or the tests below "
        "would pass for the wrong reason"
    )
    return rm


async def _paper_account_blocked_by_its_daily_loss(redis_store):
    """The PAPER mirror of the helper above: the PAPER gate is CLOSED."""
    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    await rm.update_balance(mode="paper")
    await _book_closed_trade(
        rm, "BTCUSDT", PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT, mode="paper"
    )
    assert rm.paper_stats.current_balance == pytest.approx(PAPER_BALANCE)
    assert rm.paper_stats.today_pnl == pytest.approx(
        PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT
    )
    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate must be CLOSED before the restore, or the tests below "
        "would pass for the wrong reason"
    )
    return rm


def _unreadable_records(caplog, needle):
    """The CRITICAL records that report an unreadable persisted value.

    An empty `needle` matches every one of them, which is what the
    counterweight tests want: a payload that is entirely READABLE must not
    produce an "unreadable" report at all.
    """
    return [
        r.getMessage()
        for r in caplog.records
        if "could NOT be read" in r.getMessage() and needle in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1. THE LIVE PATH: a MISSING day's PnL must not become "no loss today"
# ---------------------------------------------------------------------------


async def test_a_live_payload_missing_today_pnl_does_not_zero_the_days_pnl(
    redis_store,
):
    """A payload MISSING ``today_pnl`` must not zero the in-memory day's PnL.

    Restored as 0.0 it means "no loss today", so the daily-loss gate stops
    measuring and the LIVE gate -- the one that guards REAL money -- re-opens.
    """
    rm = await _live_account_blocked_by_its_daily_loss(redis_store)
    pnl_in_memory = rm.stats.today_pnl

    _write_payload(redis_store, rm, drop_live=("today_pnl",))

    restored = await rm.restore_state()

    assert restored is True, "the persisted state must still have been read"
    assert rm.stats.today_pnl == pytest.approx(pnl_in_memory), (
        "the MISSING persisted day's PnL was restored as 0.0: the daily-loss "
        "gate now measures NO LOSS TODAY and the day's real loss is gone"
    )
    assert rm._is_trading_allowed is False, (
        "the LIVE gate RE-OPENED on a payload that was missing the day's PnL: "
        "the daily-loss gate is a real-money control"
    )


async def test_a_live_payload_with_a_null_today_pnl_does_not_zero_the_days_pnl(
    redis_store,
):
    """A payload with ``today_pnl: null`` must not zero the day's PnL either.

    A null field is not a zero: it says the payload does not know what the
    day's PnL was. ``payload.get("today_pnl") or 0.0`` cannot tell the two
    apart, so a null field was restored as a flat day.
    """
    rm = await _live_account_blocked_by_its_daily_loss(redis_store)
    pnl_in_memory = rm.stats.today_pnl

    _write_payload(redis_store, rm, null_live=("today_pnl",))

    restored = await rm.restore_state()

    assert restored is True
    assert rm.stats.today_pnl == pytest.approx(pnl_in_memory), (
        "the NULL persisted day's PnL was restored as 0.0: the daily-loss gate "
        "now measures NO LOSS TODAY and the day's real loss is gone"
    )
    assert (
        rm._is_trading_allowed is False
    ), "the LIVE gate RE-OPENED on a payload whose day's PnL was null"


async def test_a_live_payload_missing_consecutive_losses_does_not_lower_the_streak(
    redis_store,
    caplog,
):
    """A payload MISSING ``consecutive_losses`` must not lower the streak.

    The in-memory streak stays IN FORCE -- the streak only ever grows within
    a UTC day, so it is never lowered -- and the missing field is reported as
    UNREADABLE rather than silently read as a zero.

    The report matters as much as the number: a persisted guard state that
    cannot be read is an OPERATIONAL event (CRITICAL), not a debug line. A
    restore that quietly turned "missing" into "zero" is indistinguishable
    from a restore that really found a zero.
    """
    rm = await _live_account_blocked_by_its_daily_loss(redis_store)
    streak_in_memory = rm.stats.consecutive_losses
    assert streak_in_memory == 1

    _write_payload(redis_store, rm, drop_live=("consecutive_losses",))

    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        restored = await rm.restore_state()

    assert restored is True
    assert (
        rm.stats.consecutive_losses == streak_in_memory
    ), "the MISSING persisted loss streak LOWERED the in-memory streak"
    assert _unreadable_records(caplog, "LIVE loss streak"), (
        "the MISSING persisted loss streak was silently read as a ZERO: an "
        "unreadable persisted guard state must be reported"
    )


# ---------------------------------------------------------------------------
# 2. THE COUNTERWEIGHT: a readable zero is a MEASUREMENT, not an unknown
# ---------------------------------------------------------------------------


async def test_a_readable_zero_in_the_payload_is_still_restored_as_zero(
    redis_store,
    caplog,
):
    """A payload that says ``today_pnl: 0.0`` must still restore as 0.0.

    This is the counterweight to the tests above: refusing to restore an
    unreadable value must not mean refusing to restore a GOOD one. A payload
    that says ``today_pnl: 0.0`` says the day is FLAT, and that is a fact
    the daily-loss gate has to be able to measure -- if a readable zero were
    treated as unreadable, the in-memory loss would stay in force and the
    gate would never re-open on a genuinely flat day.

    The streak keeps the existing conservative rule: a readable persisted
    streak may RAISE the in-memory streak but never lower it.
    """
    rm = await _live_account_blocked_by_its_daily_loss(redis_store)
    assert rm.stats.today_pnl < 0.0, "the day's PnL must be a LOSS before this"

    _write_payload(
        redis_store,
        rm,
        live_overrides={"today_pnl": 0.0, "consecutive_losses": 0},
    )

    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        restored = await rm.restore_state()

    assert restored is True
    assert rm.stats.today_pnl == pytest.approx(0.0), (
        "a READABLE zero was not restored: 'absent' and 'zero' are different "
        "facts and must not be confused"
    )
    assert rm.stats.consecutive_losses == 1, (
        "the max() rule on the streak must survive: a readable persisted streak "
        "may raise the in-memory streak but never lower it"
    )
    assert not _unreadable_records(caplog, ""), (
        "a READABLE zero was reported as unreadable: 'absent' and 'zero' are "
        "different facts and must not be confused"
    )


# ---------------------------------------------------------------------------
# 3. THE PAPER PATH: the SAME malformed payload, the SAME answer
# ---------------------------------------------------------------------------


async def test_a_paper_payload_missing_today_pnl_does_not_zero_the_days_pnl(
    redis_store,
):
    """The PAPER mirror of the LIVE test: the two sandboxes must agree.

    The PAPER path re-checks the PAPER gate right after the restore, on the
    restored numbers. So a missing ``today_pnl`` that was restored as 0.0
    did not just lose the day's PnL -- it RE-OPENED the PAPER gate on the
    zeroed numbers.
    """
    rm = await _paper_account_blocked_by_its_daily_loss(redis_store)
    pnl_in_memory = rm.paper_stats.today_pnl

    _write_payload(redis_store, rm, drop_paper=("today_pnl",))

    restored = await rm.restore_state()

    assert restored is True, "the persisted state must still have been read"
    assert rm.paper_stats.today_pnl == pytest.approx(pnl_in_memory), (
        "the MISSING persisted PAPER day's PnL was restored as 0.0: the "
        "PAPER daily-loss gate now measures NO LOSS TODAY"
    )
    assert (
        rm._paper_is_trading_allowed is False
    ), "the PAPER gate RE-OPENED on a payload that was missing the day's PnL"


async def test_a_paper_payload_with_a_null_today_pnl_does_not_zero_the_days_pnl(
    redis_store,
):
    """The PAPER mirror of the LIVE null test."""
    rm = await _paper_account_blocked_by_its_daily_loss(redis_store)
    pnl_in_memory = rm.paper_stats.today_pnl

    _write_payload(redis_store, rm, null_paper=("today_pnl",))

    restored = await rm.restore_state()

    assert restored is True
    assert rm.paper_stats.today_pnl == pytest.approx(pnl_in_memory), (
        "the NULL persisted PAPER day's PnL was restored as 0.0: the PAPER "
        "daily-loss gate now measures NO LOSS TODAY"
    )
    assert (
        rm._paper_is_trading_allowed is False
    ), "the PAPER gate RE-OPENED on a payload whose day's PnL was null"


async def test_a_paper_payload_missing_consecutive_losses_does_not_lower_the_streak(
    redis_store,
    caplog,
):
    """The PAPER mirror of the LIVE missing-streak test.

    The PAPER path already refused to restore a streak it could not read, so
    a missing field must be refused the same way -- and reported the same way.
    """
    rm = await _paper_account_blocked_by_its_daily_loss(redis_store)
    streak_in_memory = rm.paper_stats.consecutive_losses
    assert streak_in_memory == 1

    _write_payload(redis_store, rm, drop_paper=("consecutive_losses",))

    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        restored = await rm.restore_state()

    assert restored is True
    assert (
        rm.paper_stats.consecutive_losses == streak_in_memory
    ), "the MISSING persisted PAPER loss streak LOWERED the in-memory streak"
    assert _unreadable_records(caplog, "PAPER loss streak"), (
        "the MISSING persisted PAPER loss streak was silently read as a ZERO: "
        "an unreadable persisted guard state must be reported"
    )


async def test_a_readable_zero_in_the_paper_payload_is_still_restored_as_zero(
    redis_store,
    caplog,
):
    """The PAPER counterweight: a readable zero is still a zero."""
    rm = await _paper_account_blocked_by_its_daily_loss(redis_store)

    _write_payload(
        redis_store,
        rm,
        paper_overrides={"today_pnl": 0.0, "consecutive_losses": 0},
    )

    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        restored = await rm.restore_state()

    assert restored is True
    assert rm.paper_stats.today_pnl == pytest.approx(
        0.0
    ), "a READABLE zero was not restored in the PAPER sandbox either"
    assert not _unreadable_records(caplog, ""), (
        "a READABLE zero was reported as unreadable: 'absent' and 'zero' are "
        "different facts and must not be confused"
    )
