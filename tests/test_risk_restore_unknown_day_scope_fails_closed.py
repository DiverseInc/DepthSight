"""A persisted guard state whose SCOPE cannot be read must fail CLOSED.

THE DEFECT
----------
``_apply_persisted_guard_state`` decided which day a persisted payload
belongs to with one line::

    persisted_day_str = str(payload.get("day_str") or "")
    if persisted_day_str != current_day_str:
        logger.info("[RiskGuard] Discarding the persisted trading-guard state: "
                    "it belongs to UTC day %r and today is %r. ...")

That line collapses THREE different facts into one:

* the payload holds YESTERDAY's numbers -> discard them (correct: the daily
  rollover legitimately cleared the streak and the day's PnL);
* the payload holds TODAY's numbers -> apply them (the whole point of
  persisting them);
* the payload's day stamp CANNOT BE READ (the field is missing, null, or not
  a readable string) -> it is NOT known which of the two it is.

The third case fell into the first. A payload whose ``day_str`` was missing or
null was silently discarded "as another day", so:

* the loss streak and the day's PnL were never restored -- the counters came
  back as ZEROS;
* ``initialize_balance()`` then saw "no state was restored", re-baselined the
  counters to zero and RE-OPENED the gate -- the daily-loss gate, a real-money
  control, stopped measuring;
* and it was all logged at INFO, so an operator never saw it happen.

The PAPER sandbox had the same shape (``paper_payload.get("day_str")``), and
the PAPER base had one more: ``paper_payload.get("start_of_day_day_str") or
current_day_str`` read an ABSENT base stamp as "today", so an UNBACKED base
was installed as today's opening balance -- a denominator that may already
contain today's PnL, which makes the daily-loss ratio read LOW and
under-trip. The LIVE path refuses exactly that; the PAPER path must not
decide it differently.

WHAT IS PINNED HERE
-------------------
1. A LIVE payload MISSING ``day_str`` is an UNKNOWN scope: the gate fails
   CLOSED and it is reported at CRITICAL (not INFO).
2. A LIVE payload with ``day_str: null`` -- same.
3. A LIVE payload whose ``day_str`` is a PREVIOUS day is STILL DISCARDED:
   the correct behaviour must not change.
4. The PAPER equivalents of 1 and 3.
5. A PAPER payload with an ABSENT (or foreign) base day stamp does not get
   that stamp laundered into today's.
6. A first boot -- no persisted state at all -- is NOT blocked. The
   fail-closed state must only be reachable when there IS a persisted state
   whose scope cannot be read.

These tests drive the REAL ``RiskManager`` and the REAL ``restore_state()``
against a fake Redis, in the start-up order the controller uses (restore,
then initialize).
"""

import json
import logging

import pytest

from tests.test_per_mode_risk_gating import (
    FakeRedis,
    LIVE_BALANCE,
    PAPER_BALANCE,
    TODAY,
    _book_closed_trade,
    _dual_mode_risk_manager,
    _guard_key,
)
from tests.test_risk_restore_missing_field_fails_closed import _write_payload

# The LIVE account's day: a $600 loss against a $10,000 opening balance is a
# 6% loss, which BREACHES the 5% daily-loss limit -- so the LIVE gate is
# CLOSED before the restore.
LIVE_LOSS_THAT_BREACHES_THE_DAILY_LIMIT = -600.0

# The PAPER account's day: a $500 loss against a $5,000 opening balance is a
# 10% loss, which breaches the same 5% limit in the PAPER sandbox.
PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT = -500.0

# A day that is emphatically NOT today. Any day string that is not today
# exercises the "previous day" path.
A_PREVIOUS_DAY = "2000-01-01"


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_a_previous_process_losing_day(redis_store):
    """A previous process left a losing day behind, and its gate was closed.

    The payload in Redis is produced by the product's own ``save_state()``,
    so it is exactly what a real restart would find.
    """
    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    await _book_closed_trade(
        rm, "BTCUSDT", LIVE_LOSS_THAT_BREACHES_THE_DAILY_LIMIT, mode="live"
    )
    assert rm._is_trading_allowed is False, (
        "the LIVE gate must be CLOSED before the restart, or the tests below "
        "would pass for the wrong reason"
    )
    return rm


async def _seed_a_previous_process_losing_paper_day(redis_store):
    """The PAPER mirror: the PAPER sandbox was blocked by its own daily loss."""
    rm = await _seed_a_previous_process_losing_day(redis_store)
    await rm.update_balance(mode="paper")
    await _book_closed_trade(
        rm, "BTCUSDT", PAPER_LOSS_THAT_BREACHES_THE_DAILY_LIMIT, mode="paper"
    )
    assert rm.paper_stats.current_balance == pytest.approx(PAPER_BALANCE)
    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate must be CLOSED before the restart, or the tests below "
        "would pass for the wrong reason"
    )
    return rm


def _restart(redis_store):
    """A brand-new process against the same Redis -- the restart under test."""
    return _dual_mode_risk_manager(FakeRedis(redis_store))


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# 1. THE LIVE PATH: an unreadable day stamp is an UNKNOWN scope
# ---------------------------------------------------------------------------


async def test_a_live_payload_missing_day_str_fails_closed(redis_store, caplog):
    """A payload MISSING ``day_str`` must not be read as "another day".

    Reading it as another day discards the whole persisted state, so the
    counters come back as zeros and the daily-loss gate stops measuring. The
    scope is UNKNOWN, not previous, so the gate must fail CLOSED and the
    operator must be told at CRITICAL.
    """
    rm = await _seed_a_previous_process_losing_day(redis_store)
    _write_payload(redis_store, rm, drop_live=("day_str",))

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert rm_b._is_trading_allowed is False, (
        "the gate was left OPEN on a payload whose day stamp cannot be read: "
        "the state was silently discarded as 'another day' and the counters "
        "restarted at zero"
    )
    assert "day stamp could not be read" in (rm_b._global_gate_disable_reason or ""), (
        "the operator must be told WHY the account is blocked, got "
        f"{rm_b._global_gate_disable_reason!r}"
    )
    messages = _messages(caplog)
    assert any(
        "UNREADABLE day stamp" in m for m in messages
    ), f"the unreadable day stamp was not reported at CRITICAL, got {messages}"
    assert not any(
        "Discarding the persisted trading-guard state" in m for m in messages
    ), (
        "the payload was silently discarded as 'another day' -- an unreadable "
        "day stamp is an UNKNOWN scope, not a previous day"
    )

    # And the start-up that follows must not re-open the gate either.
    await rm_b.initialize_balance()
    assert (
        rm_b._is_trading_allowed is False
    ), "the start-up re-opened the gate on a state whose scope is unknown"


async def test_a_live_payload_with_a_null_day_str_fails_closed(redis_store, caplog):
    """A payload with ``day_str: null`` must not be read as "another day"."""
    rm = await _seed_a_previous_process_losing_day(redis_store)
    _write_payload(redis_store, rm, null_live=("day_str",))

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert (
        rm_b._is_trading_allowed is False
    ), "the gate was left OPEN on a payload whose day stamp is null"
    assert "day stamp could not be read" in (rm_b._global_gate_disable_reason or ""), (
        "the operator must be told WHY the account is blocked, got "
        f"{rm_b._global_gate_disable_reason!r}"
    )
    messages = _messages(caplog)
    assert any(
        "UNREADABLE day stamp" in m for m in messages
    ), f"the null day stamp was not reported at CRITICAL, got {messages}"
    assert not any(
        "Discarding the persisted trading-guard state" in m for m in messages
    ), (
        "the payload was silently discarded as 'another day' -- an unreadable "
        "day stamp is an UNKNOWN scope, not a previous day"
    )


async def test_a_live_payload_from_a_previous_day_is_still_discarded(
    redis_store, caplog
):
    """The CORRECT behaviour must not change: yesterday's numbers are dropped.

    This is the counterweight to the two tests above. "Another day" and
    "unknown day" are different facts, and only the second one is a fault.
    """
    rm = await _seed_a_previous_process_losing_day(redis_store)
    _write_payload(redis_store, rm, live_overrides={"day_str": A_PREVIOUS_DAY})

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.INFO, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert (
        rm_b.stats.consecutive_losses == 0
    ), "yesterday's loss streak must NOT be resurrected"
    assert rm_b.stats.today_pnl == pytest.approx(
        0.0
    ), "yesterday's day's PnL must NOT be carried into today"
    messages = _messages(caplog)
    assert any(
        "Discarding the persisted trading-guard state" in m for m in messages
    ), f"a previous day's payload must still be discarded, got {messages}"
    assert not any("UNREADABLE day stamp" in m for m in messages), (
        "a previous day's payload has a READABLE day stamp -- it must not be "
        "reported as an unknown scope"
    )


# ---------------------------------------------------------------------------
# 2. THE PAPER PATH: the same shape, the same answer
# ---------------------------------------------------------------------------


async def test_a_paper_payload_missing_day_str_fails_closed(redis_store, caplog):
    """The PAPER twin of the LIVE missing-``day_str`` test.

    The PAPER sandbox held a blocked day. A paper payload whose day stamp
    cannot be read must fail the PAPER gate CLOSED instead of silently
    restarting the paper counters at zero.
    """
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(redis_store, rm, drop_paper=("day_str",))

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert rm_b._paper_is_trading_allowed is False, (
        "the PAPER gate was left OPEN on a payload whose day stamp cannot be "
        "read: the paper counters silently restarted at zero"
    )
    messages = _messages(caplog)
    assert any("UNREADABLE day stamp" in m for m in messages), (
        f"the unreadable PAPER day stamp was not reported at CRITICAL, got "
        f"{messages}"
    )
    assert not any(
        "Discarding the persisted PAPER sandbox state" in m for m in messages
    ), (
        "the paper payload was silently discarded as 'another day' -- an "
        "unreadable day stamp is an UNKNOWN scope, not a previous day"
    )


async def test_a_paper_payload_from_a_previous_day_is_still_discarded(
    redis_store, caplog
):
    """The PAPER counterweight: a previous day's paper state is still dropped."""
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(redis_store, rm, paper_overrides={"day_str": A_PREVIOUS_DAY})

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.INFO, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert (
        rm_b.paper_stats.consecutive_losses == 0
    ), "yesterday's PAPER loss streak must NOT be resurrected"
    assert rm_b.paper_stats.today_pnl == pytest.approx(
        0.0
    ), "yesterday's PAPER day's PnL must NOT be carried into today"
    messages = _messages(caplog)
    assert any(
        "Discarding the persisted PAPER sandbox state" in m for m in messages
    ), f"a previous day's paper payload must still be discarded, got {messages}"


# ---------------------------------------------------------------------------
# 3. THE PAPER BASE: an absent day stamp must not be laundered into today
# ---------------------------------------------------------------------------


async def test_a_paper_payload_without_a_base_day_stamp_is_not_laundered_into_today(
    redis_store, caplog
):
    """An ABSENT PAPER base day stamp must not be read as "today".

    The base is the DENOMINATOR of the daily-loss ratio. A base that may
    already contain today's PnL makes that ratio read LOW and under-trip, so
    the LIVE path refuses a base with no stamp of its own -- and the PAPER
    path must refuse it the same way.
    """
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(redis_store, rm, drop_paper=("start_of_day_day_str",))

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert rm_b.paper_stats.start_of_day_day_str != TODAY, (
        "an ABSENT PAPER base day stamp was laundered into today's stamp: the "
        "base may already contain today's PnL, so the daily-loss ratio reads "
        "LOW and under-trips"
    )
    assert rm_b.paper_stats.start_of_day_balance == pytest.approx(
        0.0
    ), "the persisted PAPER base was installed without a day stamp of its own"
    messages = _messages(caplog)
    assert any(
        "NOT restoring the persisted PAPER start-of-day balance" in m for m in messages
    ), f"the refused PAPER base was not reported at CRITICAL, got {messages}"


async def test_a_paper_payload_with_a_foreign_base_day_stamp_is_not_laundered_into_today(
    redis_store, caplog
):
    """A PAPER base stamped with ANOTHER day must not be installed either."""
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(
        redis_store,
        rm,
        paper_overrides={"start_of_day_day_str": A_PREVIOUS_DAY},
    )

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert (
        rm_b.paper_stats.start_of_day_day_str != TODAY
    ), "a PAPER base stamped with another day was laundered into today's stamp"
    assert rm_b.paper_stats.start_of_day_balance == pytest.approx(0.0), (
        "the persisted PAPER base was installed even though its own day stamp "
        "says another day"
    )
    messages = _messages(caplog)
    assert any(
        "NOT restoring the persisted PAPER start-of-day balance" in m for m in messages
    ), f"the refused PAPER base was not reported at CRITICAL, got {messages}"


async def test_a_paper_payload_with_its_own_base_day_stamp_is_still_restored(
    redis_store,
):
    """The COUNTERWEIGHT: a PAPER base that says it belongs to today IS restored.

    The refusal above must not become "the paper base is never restored" --
    that would leave every paper account without a denominator.
    """
    await _seed_a_previous_process_losing_paper_day(redis_store)
    rm_b = _restart(redis_store)
    await rm_b.restore_state()

    assert (
        rm_b.paper_stats.start_of_day_day_str == TODAY
    ), "a PAPER base whose own day stamp says today must still be restored"
    assert rm_b.paper_stats.start_of_day_balance == pytest.approx(
        PAPER_BALANCE
    ), "the persisted PAPER base must still be restored"


# ---------------------------------------------------------------------------
# 4. THE FAIL-CLOSED MUST NOT REACH AN ACCOUNT THAT NEVER TRADED
# ---------------------------------------------------------------------------


async def test_a_first_boot_with_no_persisted_state_is_not_blocked(redis_store):
    """An account with no persisted guard state must still be able to trade.

    The fail-closed state is only reachable when there IS a persisted state
    whose scope cannot be read. With nothing persisted there is no scope to
    be unknown, and blocking here would brick every new account.
    """
    rm = _restart(redis_store)
    await rm.initialize_balance()

    assert rm._is_trading_allowed is True, (
        "an account with no persisted guard state must not be blocked by the "
        "unknown-scope latch"
    )
    assert rm.stats.current_balance == pytest.approx(LIVE_BALANCE)


async def test_an_unknown_scope_state_is_not_re_persisted(redis_store):
    """The unknown scope must not be LAUNDERED back into the payload.

    This process's in-memory counters are the zeros it was born with. Writing
    them over the payload would replace "the scope cannot be read" with "the
    streak really is empty" -- and the next start-up would read that as an
    empty streak and open the gate. So while the scope is unknown, nothing is
    persisted.
    """
    rm = await _seed_a_previous_process_losing_day(redis_store)
    _write_payload(redis_store, rm, drop_live=("day_str",))

    rm_b = _restart(redis_store)
    await rm_b.restore_state()
    await rm_b.save_state()

    payload = json.loads(redis_store[_guard_key(rm_b)])
    assert payload.get("consecutive_losses") == 1, (
        "the unknown-scope state was re-persisted with this process's zero "
        "counters: the next start-up would read it as an empty streak and "
        "open the gate"
    )
    assert payload.get("today_pnl") == pytest.approx(
        LIVE_LOSS_THAT_BREACHES_THE_DAILY_LIMIT
    ), "the unknown-scope state was re-persisted with this process's zero " "day's PnL"


# ---------------------------------------------------------------------------
# 5. A MISSING COUNTER IS NOT A ZERO
# ---------------------------------------------------------------------------


async def test_a_paper_payload_with_a_missing_counter_is_not_never_persisted(
    redis_store, caplog
):
    """A MISSING paper counter is UNREADABLE, not a zero.

    The paper day stamp is written as "" when the paper sandbox has never been
    persisted, so an EMPTY stamp on a section that carries no state at all is
    the "never persisted" marker, and it must not be read as an unknown scope
    (that would block every account that never paper-traded).

    But the carve-out that reads the marker used ``not
    _read_persisted_number(...)``, and that idiom cannot tell a MISSING
    counter from a genuine zero -- it reads BOTH as zero. So a paper section
    whose counters could not be read was declared "never persisted" and thrown
    away: the unreadable paper loss streak was silently discarded and the
    paper gate was left to re-open on zeros. A missing field is UNREADABLE,
    not a zero, and the marker is only a FACT when every counter is READABLE
    *and* zero.
    """
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(
        redis_store,
        rm,
        drop_paper=("today_pnl",),
        paper_overrides={
            "day_str": "",
            "consecutive_losses": 0,
            "start_of_day_balance": 0.0,
            "start_of_day_day_str": "",
        },
    )

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert rm_b._guard_state_scope_unknown is True, (
        "a PAPER section whose counters could not be read was declared 'never "
        "persisted': its unreadable loss streak was thrown away and the state "
        "was laundered back into the payload as a known, empty one"
    )
    assert rm_b._paper_is_trading_allowed is False, (
        "the PAPER gate was left OPEN on a section whose scope and counters "
        "could not be read"
    )

    # And the next PAPER balance update must not re-open it on zeros either.
    await rm_b.update_balance(mode="paper")
    assert rm_b._paper_is_trading_allowed is False, (
        "a later PAPER balance update re-opened the gate on zeroed counters: "
        "the unreadable paper state was discarded as 'never persisted' and "
        "re-anchored a fresh base on top of it"
    )


async def test_a_paper_payload_with_an_empty_day_stamp_and_zero_counters_is_never_persisted(
    redis_store,
):
    """The COUNTERWEIGHT: the "never persisted" marker must keep working.

    An EMPTY paper day stamp on a section whose counters are all READABLE and
    zero is the product's own "the paper sandbox was never persisted" marker.
    Reading it as an unknown scope would block every account that never
    paper-traded -- the opposite failure, and a much more common one than a
    corrupt payload.
    """
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(
        redis_store,
        rm,
        paper_overrides={
            "day_str": "",
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": 0.0,
            "start_of_day_day_str": "",
        },
    )

    rm_b = _restart(redis_store)
    await rm_b.restore_state()

    assert rm_b._guard_state_scope_unknown is False, (
        "an EMPTY paper day stamp on a section whose counters are all readable "
        "and zero is the 'never persisted' marker, not an unknown scope: "
        "blocking here would lock out every account that never paper-traded"
    )
    assert rm_b.paper_stats.consecutive_losses == 0
    assert rm_b.paper_stats.today_pnl == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. THE PAPER SCOPE IS A SEPARATE QUESTION FROM THE LIVE ONE
# ---------------------------------------------------------------------------


async def test_a_corrupt_paper_section_is_not_hidden_by_a_previous_day_live_section(
    redis_store, caplog
):
    """The live section's early return must not hide the paper section.

    The restore short-circuits on a live payload that belongs to another day
    and returns before the paper section is looked at. But the paper section
    carries its OWN day stamp: a paper section whose stamp cannot be read is
    an UNKNOWN paper scope even when the live section says "another day".
    Skipping it left the paper counters at the process-born zeros, and the
    PAPER gate re-opened on them at the next paper balance update.
    """
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(
        redis_store,
        rm,
        live_overrides={"day_str": A_PREVIOUS_DAY},
        drop_paper=("day_str",),
    )

    rm_b = _restart(redis_store)
    with caplog.at_level(logging.CRITICAL, logger="bot_module.risk_manager"):
        await rm_b.restore_state()

    assert rm_b._guard_state_scope_unknown is True, (
        "the unreadable PAPER day stamp was hidden behind the live section's "
        "'another day' early return: the paper scope is UNKNOWN and must fail "
        "closed like every other unknown scope"
    )
    assert rm_b._paper_is_trading_allowed is False, (
        "the PAPER gate was left OPEN on a section whose scope could not be "
        "read"
    )

    # And a later PAPER balance update must not re-open it on zeros.
    await rm_b.update_balance(mode="paper")
    assert rm_b._paper_is_trading_allowed is False, (
        "a later PAPER balance update re-opened the gate on zeroed counters: "
        "the unreadable paper section was skipped entirely"
    )


async def test_a_previous_day_paper_section_is_still_discarded_with_a_previous_day_live_section(
    redis_store,
):
    """The COUNTERWEIGHT: a READABLE previous-day paper stamp is still dropped.

    The paper section is only re-examined because its SCOPE must be read. A
    stamp that reads as another day is another day, and yesterday's paper
    numbers must not be resurrected.
    """
    rm = await _seed_a_previous_process_losing_paper_day(redis_store)
    _write_payload(
        redis_store,
        rm,
        live_overrides={"day_str": A_PREVIOUS_DAY},
        paper_overrides={"day_str": A_PREVIOUS_DAY},
    )

    rm_b = _restart(redis_store)
    await rm_b.restore_state()

    assert (
        rm_b._guard_state_scope_unknown is False
    ), "a READABLE previous-day paper stamp is another day, not an unknown scope"
    assert (
        rm_b.paper_stats.consecutive_losses == 0
    ), "yesterday's PAPER loss streak must NOT be resurrected"
    assert rm_b.paper_stats.today_pnl == pytest.approx(
        0.0
    ), "yesterday's PAPER day's PnL must NOT be carried into today"
