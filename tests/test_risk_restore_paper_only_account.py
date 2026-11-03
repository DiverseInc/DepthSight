"""A KEYLESS PAPER-ONLY account's persisted guard state must survive a deploy.

THE DEFECT
----------
Before the paper/live split there was ONE sandbox of counters, and it was
persisted at the TOP LEVEL of the payload. The split moved the paper counters
under a ``"paper"`` key and left the live counters at the top level -- and the
restore then read ONLY the ``"paper"`` key for the paper sandbox.

For an account with a live account behind it, that is safe: the top-level
counters are (at worst) a mix of the live and the paper counters, and they
stay in the LIVE sandbox, which is the tight side for real money.

For a KEYLESS PAPER-ONLY account it is not safe, and it is not even true:
such an account cannot make a LIVE trade at all, so the shared counters it
persisted were ITS OWN counters -- its paper loss streak and its paper day's
PnL. The restore put them in the LIVE sandbox, where nothing reads them:

* the PAPER sandbox started at zero;
* ``_initialize_paper_balance()`` opened the PAPER gate on those zeros;
* the next ``save_state()`` wrote the zeros back.

So the loss streak that was blocking the account was lost on the deploy that
carried the split -- exactly the production incident the restore exists to
prevent (``controller.py``: "a routine deploy moved consecutive_losses from
10 to 0 and the gate from blocked to allowed").

The diff's own comment said "the paper sandbox was never persisted before".
For this account class that was FALSE, and it is corrected here.

WHAT IS PINNED HERE
-------------------
1. A PRE-CHANGE payload (no ``"paper"`` key) for a keyless paper-only account
   restores into the PAPER sandbox, and the PAPER gate stays CLOSED.
2. The next ``save_state()`` persists the streak, not zeros.
3. A payload whose LIVE section belongs to another UTC day does NOT discard a
   PAPER section that is stamped TODAY: the paper section carries its own day
   stamp, and its own stamp decides.
4. A persisted VALUE that cannot be read is UNKNOWN, and an UNKNOWN value
   fails CLOSED at start-up -- where the in-memory value is the process-born
   zero, not a measurement.
5. A day stamp that is a non-blank string but NOT a calendar day is UNREADABLE,
   not "another day".
6. The COUNTERWEIGHT: a LIVE account's pre-change payload still restores into
   the LIVE sandbox, and its PAPER counters stay at zero.
7. The RESERVING placeholder carries the signal's OWN mode, so a paper
   signal's close can never be booked into the live counters.

These tests drive the REAL ``RiskManager``, the REAL ``restore_state()`` and
the REAL controller method against a fake Redis. The end state asserted is the
product's, not a stub's.
"""

import json
import logging

import pytest

from tests.test_ml_confirmation_fail_open_is_not_silent import (
    _StopHere,
    _build_controller,
    _make_signal,
)
from tests.test_per_mode_risk_gating import (
    LIVE_BALANCE,
    MAX_CONSECUTIVE_LOSSES,
    PAPER_BALANCE,
    FakeRedis,
    _guard_key,
    _paper_only_risk_manager,
)

TODAY = "2026-10-27"
YESTERDAY = "2026-10-26"

# A streak over the limit (3), so the PAPER gate must be CLOSED when it is
# restored, and a day's PnL that is a real loss.
PAPER_STREAK = MAX_CONSECUTIVE_LOSSES + 4
PAPER_LOSS = -500.0


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


def _keyless_paper_account(redis_store, day=TODAY, user_id=1):
    """A real RiskManager for a KEYLESS paper-only account.

    ``bot_runner.py`` builds the paper-only controller with ``api_key_id=None``
    and no live executor, and the controller copies ``api_key_id`` onto the
    risk manager. So "keyless" means ``api_key_id is None``: that is the
    account class whose guard state the deploy must not lose.
    """
    rm = _paper_only_risk_manager(
        FakeRedis(redis_store), day=day, user_id=user_id, api_key_id=None
    )
    assert rm.api_key_id is None
    assert rm.executor is None
    return rm


def _write_payload(redis_store, rm, payload):
    """Put a payload into Redis exactly as ``save_state`` would."""
    key = _guard_key(rm)
    redis_store[key] = json.dumps(payload)
    return payload


def _legacy_payload(day_str=TODAY, **overrides):
    """A payload written BEFORE the paper/live split.

    It has ONE sandbox of counters, at the TOP LEVEL, and NO ``"paper"`` key.
    """
    payload = {
        "version": 2,
        "day_str": day_str,
        "consecutive_losses": PAPER_STREAK,
        "today_pnl": PAPER_LOSS,
        "start_of_day_balance": PAPER_BALANCE,
        "start_of_day_day_str": day_str,
    }
    payload.update(overrides)
    return payload


def _paper_section(day_str=TODAY, **overrides):
    """A POST-split payload's PAPER section."""
    section = {
        "day_str": day_str,
        "consecutive_losses": PAPER_STREAK,
        "today_pnl": PAPER_LOSS,
        "start_of_day_balance": PAPER_BALANCE,
        "start_of_day_day_str": day_str,
    }
    section.update(overrides)
    return section


def _persisted(redis_store, rm):
    return json.loads(redis_store[_guard_key(rm)])


# ---------------------------------------------------------------------------
# 1. THE CENTRAL DEFECT: a pre-change payload for a KEYLESS account
# ---------------------------------------------------------------------------


async def test_a_pre_change_payload_restores_into_the_paper_sandbox(redis_store):
    """The legacy top level WAS this account's own state; it must be restored.

    On the unfixed code the top-level counters went into the LIVE sandbox,
    the PAPER sandbox stayed at zero, and the PAPER gate opened on those
    zeros -- so the account that was blocked by a losing streak was allowed
    to trade again, and the next save wrote the zeros back for good.
    """
    rm = _keyless_paper_account(redis_store)
    _write_payload(redis_store, rm, _legacy_payload())

    restored = await rm.restore_state()

    assert restored is True, "the persisted state must still have been read"
    assert rm.paper_stats.consecutive_losses == PAPER_STREAK, (
        "the pre-change payload's loss streak was NOT restored into the PAPER "
        "sandbox: for an account with no live account behind it the top-level "
        "counters WERE its own paper counters. The account was blocked by a "
        f"streak of {PAPER_STREAK} and the restore read it as zero."
    )
    assert rm.paper_stats.today_pnl == pytest.approx(PAPER_LOSS)

    await rm.initialize_balance()

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a payload that carried the account's own "
        f"loss streak of {PAPER_STREAK}: the streak is over the limit of "
        f"{MAX_CONSECUTIVE_LOSSES}, so the account must stay blocked."
    )


async def test_a_pre_change_payload_is_not_laundered_by_the_next_save(redis_store):
    """The restored streak must survive the next ``save_state()``.

    On the unfixed code the PAPER counters were zero, so the next save wrote
    ``consecutive_losses: 0`` into the payload -- the block was destroyed
    permanently, not just for one process.
    """
    rm = _keyless_paper_account(redis_store)
    _write_payload(redis_store, rm, _legacy_payload())

    await rm.restore_state()
    await rm.initialize_balance()
    await rm.save_state()

    persisted = _persisted(redis_store, rm)
    assert isinstance(persisted.get("paper"), dict), (
        "the next save must carry the PAPER section, or the paper counters "
        "are gone for good"
    )
    assert persisted["paper"]["consecutive_losses"] == PAPER_STREAK, (
        "the next save wrote the PAPER counters as ZERO: the loss streak that "
        "was blocking the account has been laundered into the payload."
    )


async def test_the_published_snapshot_shows_the_restored_streak(redis_store):
    """What the operator sees must be the account's real state.

    ``published_account_snapshot()`` publishes the PAPER numbers for an
    account with no live account behind it, so it is the surface the operator
    reads. On the unfixed code it reported an empty streak and an open gate
    for an account that was blocked.
    """
    rm = _keyless_paper_account(redis_store)
    _write_payload(redis_store, rm, _legacy_payload())

    await rm.restore_state()
    await rm.initialize_balance()

    snapshot = rm.published_account_snapshot()
    assert snapshot["consecutive_losses"] == PAPER_STREAK, (
        f"the published snapshot reports an empty loss streak: {snapshot!r}"
    )
    assert snapshot["is_trading_allowed"] is False, (
        f"the published snapshot reports the account as ALLOWED: {snapshot!r}"
    )


# ---------------------------------------------------------------------------
# 2. The PAPER section is restored on its OWN scope
# ---------------------------------------------------------------------------


async def test_a_paper_section_stamped_today_survives_a_stale_live_section(
    redis_store,
):
    """The live section's day stamp says nothing about the paper section's.

    The live section carries its own day stamp and the paper section carries
    its own. When the live section belongs to another UTC day, the restore
    returned early -- after only SCOPE-CHECKING the paper sub-payload -- so a
    paper section stamped TODAY was never applied and never latched: the
    paper counters restarted at zero, the PAPER gate opened on them, and the
    next save overwrote the payload.
    """
    rm = _keyless_paper_account(redis_store)
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": YESTERDAY,
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": 0.0,
            "start_of_day_day_str": "",
            "paper": _paper_section(TODAY),
        },
    )

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm.paper_stats.consecutive_losses == PAPER_STREAK, (
        "the PAPER section stamped TODAY was discarded because the LIVE "
        "section belonged to another UTC day. The paper section carries its "
        "OWN day stamp, and its own stamp decides."
    )
    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a paper section that carries today's "
        f"loss streak of {PAPER_STREAK}"
    )

    await rm.save_state()
    persisted = _persisted(redis_store, rm)
    assert persisted["paper"]["consecutive_losses"] == PAPER_STREAK, (
        "the next save wrote the PAPER counters as ZERO: the paper block was "
        "laundered into the payload."
    )


# ---------------------------------------------------------------------------
# 3. An unreadable persisted VALUE fails closed at START-UP
# ---------------------------------------------------------------------------


async def test_an_unreadable_persisted_streak_fails_closed_at_start_up(
    redis_store,
):
    """"Keep the in-memory value" is only fail-closed if it is a MEASUREMENT.

    At start-up the in-memory counters are the zeros this process was born
    with, so "keep them" means "the loss streak is zero" -- and the streak
    guard then measures nothing and the gate re-opens. The existing
    fail-closed tests never saw this: they restored over an account whose
    in-memory state was already populated, so the in-memory value WAS
    meaningful there.

    An unreadable persisted value is UNKNOWN, and unknown must never be more
    permissive than known.
    """
    rm = _keyless_paper_account(redis_store)
    paper = _paper_section(TODAY)
    paper.pop("consecutive_losses")
    paper["today_pnl"] = 0.0
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": PAPER_BALANCE,
            "start_of_day_day_str": TODAY,
            "paper": paper,
        },
    )

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a payload whose loss streak could not be "
        "read: the in-memory streak was the process-born zero, so the guard "
        "measured nothing. An unreadable persisted value is UNKNOWN, and "
        "unknown must fail closed."
    )
    assert rm._guard_state_is_unknown() is True, (
        "an unreadable persisted value must latch the fail-closed state, so "
        "the gate cannot be re-opened by the start-up machinery"
    )


async def test_an_unreadable_persisted_value_is_not_re_persisted(redis_store):
    """Writing the in-memory zeros would LAUNDER an unknown state.

    The next start-up would read the payload as "the streak really is empty"
    and open the gate.
    """
    rm = _keyless_paper_account(redis_store)
    paper = _paper_section(TODAY)
    paper.pop("today_pnl")
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": PAPER_BALANCE,
            "start_of_day_day_str": TODAY,
            "paper": paper,
        },
    )

    await rm.restore_state()
    before = _persisted(redis_store, rm)
    await rm.save_state()
    after = _persisted(redis_store, rm)

    assert after == before, (
        "the state was re-persisted while a persisted value was UNREADABLE: "
        "the in-memory counters are UNKNOWN, not zero, and writing them "
        "launders the unknown state into the payload."
    )


async def test_a_null_paper_section_is_corruption_not_a_legacy_payload(
    redis_store,
):
    """A key that is PRESENT but unreadable is not an ABSENT key.

    ``payload.get("paper")`` returns ``None`` for both, so the difference has
    to be made explicitly: an ABSENT "paper" key is a payload written before
    the split, and a key that is present but holds ``null`` is CORRUPTION --
    the paper section existed and its contents cannot be read, so the paper
    scope is UNKNOWN and the paper gate must fail closed. Reading it as "the
    paper sandbox was never persisted" would restore the paper counters as
    ZEROS and re-open a gate that was closed.
    """
    rm = _keyless_paper_account(redis_store)
    payload = _legacy_payload()
    payload["paper"] = None
    _write_payload(redis_store, rm, payload)

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a payload whose PAPER section is null: "
        "the section existed and its contents cannot be read, so the paper "
        "scope is UNKNOWN and unknown must fail closed."
    )
    assert rm._guard_state_is_unknown() is True


async def test_an_unreadable_live_value_fails_closed_at_start_up(redis_store):
    """The LIVE daily-loss and streak guards must not measure nothing.

    This is the LIVE half of the same defect: an unreadable persisted streak
    only "left the in-memory value in force", and at start-up the in-memory
    streak is the process-born zero -- so `_check_risk_limits()` measured
    nothing and re-opened the LIVE gate on it. The live account's balance is
    healthy here, so the gate opens on the unfixed code and MUST NOT open on
    the fixed one.
    """
    from tests.test_per_mode_risk_gating import _dual_mode_risk_manager

    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    payload = {
        "version": 2,
        "day_str": TODAY,
        "consecutive_losses": None,
        "today_pnl": 0.0,
        "start_of_day_balance": LIVE_BALANCE,
        "start_of_day_day_str": TODAY,
    }
    _write_payload(redis_store, rm, payload)

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._is_trading_allowed is False, (
        "the LIVE gate RE-OPENED on a payload whose persisted loss streak "
        "could not be read: the in-memory streak was the process-born zero, "
        "so the streak guard and the daily-loss guard measured NOTHING. An "
        "unreadable persisted value is UNKNOWN, and unknown must fail closed."
    )
    assert rm._guard_state_is_unknown() is True, (
        "an unreadable persisted LIVE value must latch the fail-closed state"
    )


# ---------------------------------------------------------------------------
# 4. A day stamp that is not a calendar day is UNREADABLE, not "another day"
# ---------------------------------------------------------------------------


async def test_an_invalid_paper_day_stamp_is_not_read_as_another_day(
    redis_store,
):
    """A non-blank string that is not a date cannot be compared with today.

    Reading it as "another day" discards the counters, restores them as
    ZEROS and re-opens the gate that was closed -- the same fail-open the
    missing and null stamps were fixed for, reached through a third door.
    """
    rm = _keyless_paper_account(redis_store)
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": PAPER_BALANCE,
            "start_of_day_day_str": TODAY,
            "paper": _paper_section("not-a-calendar-day"),
        },
    )

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a payload whose PAPER day stamp is not a "
        "calendar day: an unreadable stamp is UNKNOWN, not 'another day', and "
        "unknown must fail closed."
    )
    assert rm._guard_state_is_unknown() is True


async def test_an_invalid_live_day_stamp_is_not_read_as_another_day(
    redis_store, caplog
):
    """The LIVE section's stamp is unreadable too, so it must fail closed.

    "Anything that always reports OK is worse than nothing": an unreadable
    stamp must be distinguishable from "another day", and the state must not
    be re-persisted as if it had been believed.
    """
    rm = _keyless_paper_account(redis_store)
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": "not-a-calendar-day",
            "consecutive_losses": PAPER_STREAK,
            "today_pnl": PAPER_LOSS,
            "start_of_day_balance": PAPER_BALANCE,
            "start_of_day_day_str": TODAY,
        },
    )

    with caplog.at_level(logging.CRITICAL):
        await rm.restore_state()

    assert rm._guard_state_is_unknown() is True, (
        "a LIVE day stamp that is not a calendar day must latch the "
        "fail-closed state, not be discarded as 'another day'"
    )
    before = _persisted(redis_store, rm)
    await rm.save_state()
    assert _persisted(redis_store, rm) == before, (
        "the state was re-persisted while its day stamp was UNREADABLE"
    )


# ---------------------------------------------------------------------------
# 5. THE COUNTERWEIGHT: a LIVE account is unchanged
# ---------------------------------------------------------------------------


async def test_a_live_accounts_pre_change_payload_still_restores_live(redis_store):
    """The top-level counters of a LIVE account must stay in the LIVE sandbox.

    For an account with a live account behind it, the top-level counters are
    (at worst) a mix of the live and the paper counters, so they stay live.
    Putting them in the paper sandbox as well would re-introduce the very
    bug the split fixed: a paper loss closing the LIVE gate.
    """
    from tests.test_per_mode_risk_gating import _dual_mode_risk_manager

    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    _write_payload(redis_store, rm, _legacy_payload())

    await rm.restore_state()

    assert rm.stats.consecutive_losses == PAPER_STREAK, (
        "a LIVE account's pre-change payload must still restore into the LIVE "
        "sandbox: there is NO guard reset on deploy"
    )
    assert rm.paper_stats.consecutive_losses == 0, (
        "a LIVE account's pre-change payload must NOT be projected onto the "
        "PAPER sandbox: the live counters would then carry a paper loss"
    )


# ---------------------------------------------------------------------------
# 6. The RESERVING placeholder carries the signal's OWN mode
# ---------------------------------------------------------------------------


async def test_the_reserving_placeholder_carries_the_signals_own_mode():
    """A position that names no mode books its close into the LIVE counters.

    ``_process_signal`` inserts a zero-quantity ``RESERVING`` placeholder
    before it knows the mode, so the placeholder carried ``LivePosition``'s
    default of "live". The emergency stop flattens ``RESERVING`` positions
    too, and a close is booked into the sandbox its POSITION names -- so a
    PAPER signal's placeholder moved the LIVE loss streak and could close the
    LIVE gate on a simulated loss.

    AGENTS.md: "Guards on real-money paths must fail in the same direction as
    the thing they guard... Prefer `if mode == "paper": <loose>` over
    `if mode == "live": <tight>`." An unknown mode must never end up in the
    loose sandbox.
    """
    controller = _build_controller(use_ml_confirmation=False)
    captured = {}

    async def _capture_then_stop(*args, **kwargs):
        placeholder = controller._active_position_get("BTCUSDT", "futures")
        captured["status"] = getattr(placeholder, "status", None)
        captured["mode"] = getattr(placeholder, "mode", None)
        raise _StopHere()

    controller._get_market_info = _capture_then_stop
    signal = _make_signal()

    await controller._process_signal(
        signal, {"market_type": "futures", "last_price": 92_000.0}
    )

    assert captured.get("status") == "RESERVING", (
        "the ramp-stop did not observe the RESERVING placeholder: the test "
        f"did not reach the code under test. Captured: {captured!r}"
    )
    assert captured.get("mode") == "paper", (
        "the RESERVING placeholder still carries LivePosition's default "
        f"mode {captured.get('mode')!r} instead of the signal's own mode "
        "'paper'. A close is booked into the sandbox its position names, so an "
        "unstamped placeholder books a PAPER close into the LIVE counters."
    )


# ---------------------------------------------------------------------------
# 7. A persisted number that is not FINITE is UNREADABLE, not a measurement
# ---------------------------------------------------------------------------


async def test_a_non_finite_persisted_day_pnl_fails_closed_at_start_up(
    redis_store,
):
    """NaN is not a day's PnL -- it is an arithmetic accident.

    ``json.dumps`` writes ``float("nan")`` as the literal ``NaN`` and
    ``json.loads`` reads it straight back, so a non-finite value survives a
    save/restore round trip and reaches the guard EXACTLY as a number would.

    And then every comparison against it is silently FALSE: ``nan < 0`` is
    False, so the loss guard reads "no loss today" while the persisted number
    says something else entirely. Measured on the code before this fix: the
    same payload with ``-5000.0`` closed the PAPER gate, and with ``NaN`` left
    it OPEN with no unknown-state latch -- the guard was measuring nothing
    and reporting OK.

    An unreadable value is UNKNOWN, and unknown must never be more permissive
    than known.
    """
    rm = _keyless_paper_account(redis_store)
    paper = _paper_section(TODAY)
    paper["today_pnl"] = float("nan")
    paper["consecutive_losses"] = 0
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": PAPER_BALANCE,
            "start_of_day_day_str": TODAY,
            "paper": paper,
        },
    )

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a payload whose day's PnL is NaN: a "
        "non-finite number is not a measurement, and every comparison against "
        "it is FALSE, so the loss guard measured NOTHING while reporting OK"
    )
    assert rm._guard_state_is_unknown() is True, (
        "a non-finite persisted value must latch the fail-closed state, so the "
        "gate cannot be re-opened by the start-up machinery"
    )


async def test_an_infinite_persisted_day_pnl_fails_closed_at_start_up(
    redis_store,
):
    """The other non-finite shape: +/-Infinity.

    ``int(float("inf"))`` raises OverflowError, which is NOT a ValueError, so
    a persisted INFINITE LOSS STREAK used to escape the reader as an
    exception instead of an unknown -- an exception thrown out of the restore
    is not a fail-closed restore.
    """
    rm = _keyless_paper_account(redis_store)
    paper = _paper_section(TODAY)
    paper["consecutive_losses"] = float("inf")
    paper["today_pnl"] = float("-inf")
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": PAPER_BALANCE,
            "start_of_day_day_str": TODAY,
            "paper": paper,
        },
    )

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._paper_is_trading_allowed is False, (
        "the PAPER gate RE-OPENED on a payload whose counters are infinite: "
        "an infinite number is not a measurement"
    )
    assert rm._guard_state_is_unknown() is True


async def test_a_non_finite_persisted_live_day_pnl_fails_closed_at_start_up(
    redis_store,
):
    """The LIVE half: a NaN day's PnL must not blind the LIVE loss guard.

    The live account's balance is healthy here, so the gate opens on the
    unfixed code and MUST NOT open on the fixed one.
    """
    from tests.test_per_mode_risk_gating import _dual_mode_risk_manager

    rm = _dual_mode_risk_manager(FakeRedis(redis_store))
    _write_payload(
        redis_store,
        rm,
        {
            "version": 2,
            "day_str": TODAY,
            "consecutive_losses": 0,
            "today_pnl": float("nan"),
            "start_of_day_balance": LIVE_BALANCE,
            "start_of_day_day_str": TODAY,
        },
    )

    await rm.restore_state()
    await rm.initialize_balance()

    assert rm._is_trading_allowed is False, (
        "the LIVE gate RE-OPENED on a payload whose day's PnL is NaN: the "
        "daily-loss guard compared NaN against the limit, every comparison "
        "was FALSE, and the guard measured NOTHING"
    )
    assert rm._guard_state_is_unknown() is True, (
        "a non-finite persisted LIVE value must latch the fail-closed state"
    )
