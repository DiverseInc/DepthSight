"""Regression tests: the daily-loss base must belong to the CURRENT UTC day.

THE DEFECT (measured in production on 2026-10-22, account ``diverseinc``)
-------------------------------------------------------------------------
The daily-loss gate at ``risk_manager.py`` is a RATIO::

    daily_loss_pct = abs(today_pnl / start_of_day_balance)

``start_of_day_balance`` is the day's opening balance, so it is only
meaningful for the day it was captured on. But the persisted payload recorded
only ``day_str`` -- the day the STREAK and the day's PnL belong to -- and never
which day the BASE belonged to. The two were independent quantities, so a base
captured on one day could be accepted under a later day's stamp.

Worse, ``_apply_persisted_guard_state`` then stamped
``last_known_day_str = current_day_str`` unconditionally, which did two
harmful things at once: it re-blessed the stale number as belonging to today,
and it made ``initialize_balance``'s ``guard_state_restored_today`` branch skip
the daily re-baseline. So the base survived indefinitely, and every subsequent
restart re-stamped it. Measured on 2026-10-22:

    stale base 1270.5851573304262 (captured 2026-10-20T00:20Z) -> gate 5.75%
    the true base at the 00:01Z rollover was 905.2300           -> true  8.07%

The gate UNDER-TRIPS: at the stale base it permits any daily loss below
$63.53, where a correct 5% of 905.23 is $45.26 -- $18.27 a day of extra
permitted loss, silently. The account was blocked only by luck, because both
figures exceed the 5% limit.

The same function also failed OPEN in two adjacent ways, pinned here as well:

* ``if self.stats.start_of_day_balance > 1e-9:`` wrapped BOTH the drawdown and
  the daily-loss checks, so a base near zero silently skipped BOTH, with no
  log -- inside a function whose own comment demands failing closed on unknown
  state.
* a missing/evicted Redis key was indistinguishable from a first boot, so
  ``restore_state`` reported "nothing lost" and ``initialize_balance`` zeroed
  ``today_pnl`` and ``consecutive_losses`` and re-opened the gate.

These tests drive the REAL ``RiskManager`` with only the exchange handle and
the clock faked. The end state asserted is the product's, not a stub's.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module import risk_manager as risk_manager_module
from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor

# The operator-facing reasons are asserted as LITERAL fragments rather than
# imported from the module. Importing them would make this file fail to COLLECT
# against the unfixed base commit, and a collection error is not a RED test --
# it proves nothing about the defect. Pinning the text also pins the wire
# contract an operator sees, independently of the implementation's plumbing.
BASE_UNKNOWN_REASON_FRAGMENT = "the opening balance for the current UTC day is unknown"
STATE_LOST_REASON_FRAGMENT = "traded before"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses.

    The store is a dict and is deliberately SHARED between the RiskManagers
    below: handing the same store to a second RiskManager is exactly what
    "the bot restarted" means here. A fake that kept its state inside the
    RiskManager could not express a restart at all.
    """

    def __init__(self, store=None):
        self.store = {} if store is None else store
        self.set_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.set_calls.append((key, value))
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


class ExplodingRedis(FakeRedis):
    """Redis that fails on read -- the fail-closed branch."""

    async def get(self, key):
        raise ConnectionError("redis is down")


# The dedicated namespace for the bot's private guard state, written as a
# LITERAL so this file pins the wire contract independently of the
# implementation's plumbing.
GUARD_STATE_NAMESPACE = "depthsight:state:risk"

# The production numbers, so the test fails for the RIGHT reason if the defect
# ever comes back.
STALE_BASE = 1270.5851573304262   # captured 2026-10-20T00:20Z
TRUE_BASE = 905.2300              # the correct base at the 2026-10-22 rollover
DAY_ONE = "2026-10-20"
DAY_TWO = "2026-10-21"
DAY_THREE = "2026-10-22"


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


def _risk_manager(redis_client, day, balance=10000.0, user_id=1, api_key_id=None):
    """A real RiskManager with only the exchange handle and the clock faked.

    The clock is faked by overriding ``_get_current_day_start_info``, which
    every day-sensitive path funnels through: ``_current_utc_day_str()``,
    ``_check_and_reset_daily_stats()`` and ``initialize_balance()``. That makes
    a multi-day sequence expressible without sleeping for two days.
    """
    executor = AsyncMock(spec=ExchangeExecutor)
    executor.get_account_balance.return_value = {
        "USDT": {"free": f"{balance:.8f}", "locked": "0.00"}
    }
    rm = RiskManager(
        executor=executor,
        paper_executor=None,
        user_id=user_id,
        db_session=None,
        user_settings={"risk_management": {"maxConsecutiveLosses": 5}},
        api_key_name="paper",
    )
    rm.redis_client = redis_client
    rm.api_key_id = api_key_id
    rm.executor = executor
    rm._fake_day = day
    rm._get_current_day_start_info = lambda: (
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
        rm._fake_day,
    )
    return rm


def _guard_key(rm):
    return f"{GUARD_STATE_NAMESPACE}:{rm.user_id}:{rm.api_key_id}"


def _seed_payload(rm, store, *, day_str, base, base_day_str, today_pnl=-73.03):
    """Put a guard payload in Redis directly, as a previous process left it.

    Written by hand rather than produced by ``save_state`` on purpose: the
    defect is that the READER cannot tell which day the base belongs to, so the
    test must hand the reader exactly that ambiguity. The extra field is ignored
    by the unfixed code, which is what makes this test go red there.
    """
    store[_guard_key(rm)] = json.dumps(
        {
            "version": 2,
            "day_str": day_str,
            "consecutive_losses": 0,
            "today_pnl": today_pnl,
            "start_of_day_balance": base,
            "start_of_day_day_str": base_day_str,
        }
    )
    assert _guard_key(rm) in store, "the seeded payload must actually be present"


async def _start_on_day(redis, store, day, balance, *, order="restore-first"):
    """Run one process start for `day`, in the controller's call order.

    The controller calls ``restore_state()`` (controller.py:1189) and then
    ``initialize_balance()`` (:1205), which is "restore-first". "init-first" is
    pinned too, because the invariant has to hold in BOTH orders.
    """
    rm = _risk_manager(redis, day, balance=balance)
    if order == "restore-first":
        await rm.restore_state()
        await rm.initialize_balance()
    else:
        await rm.initialize_balance()
        await rm.restore_state()
    return rm


# ---------------------------------------------------------------------------
# THE regression: a day-1 base must not still be in force on day 3
# ---------------------------------------------------------------------------


async def test_a_base_captured_on_day_one_is_not_still_in_force_on_day_three(
    redis_store,
):
    """THE regression, reproducing the measured production failure.

    A base captured on 2026-10-20 must not still be the daily-loss denominator
    on 2026-10-22 after two restarts. Each restart carries a payload whose
    ``day_str`` is TODAY (so the streak/PnL day check legitimately passes) but
    whose base belongs to DAY ONE -- precisely the ambiguity the unfixed reader
    had no way to resolve.
    """
    redis = FakeRedis(redis_store)
    day_one = _risk_manager(redis, DAY_ONE, balance=STALE_BASE)
    await day_one.initialize_balance()
    assert day_one.stats.start_of_day_balance == pytest.approx(STALE_BASE)

    day_three = None
    for day in (DAY_TWO, DAY_THREE):
        probe = _risk_manager(redis, day, balance=TRUE_BASE)
        _seed_payload(
            probe,
            redis_store,
            day_str=day,
            base=STALE_BASE,
            base_day_str=DAY_ONE,
        )
        day_three = await _start_on_day(redis, redis_store, day, TRUE_BASE)

    assert day_three.stats.start_of_day_balance == pytest.approx(
        TRUE_BASE
    ), (
        f"the daily-loss base captured on {DAY_ONE} (${STALE_BASE:.2f}) was still "
        f"in force on {DAY_THREE}. It makes the daily-loss gate measure against "
        "the wrong denominator, which reads LOW: the gate reports 5.75% where "
        "the true figure is 8.07%, so it permits extra loss silently."
    )


async def test_the_stale_base_makes_the_daily_loss_gate_under_trip(redis_store):
    """The user-visible harm, in the production numbers.

    A correct base makes the gate STRICTER. This asserts the measured
    difference so a fix cannot make the gate pass by some other route.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_THREE,
        base=STALE_BASE,
        base_day_str=DAY_ONE,
        today_pnl=-73.03,
    )
    rm = await _start_on_day(redis, redis_store, DAY_THREE, TRUE_BASE)

    gate_pct = abs(rm.stats.today_pnl / rm.stats.start_of_day_balance) * 100
    true_pct = abs(rm.stats.today_pnl / TRUE_BASE) * 100

    assert gate_pct == pytest.approx(true_pct), (
        f"the gate measured the loss at {gate_pct:.4f}% against a base of "
        f"${rm.stats.start_of_day_balance:.2f}, but the day's opening balance "
        f"was ${TRUE_BASE:.2f} so the loss is {true_pct:.4f}%. Measuring "
        "against a stale base reads LOW and under-trips."
    )
    assert gate_pct > 5.0, (
        "with the correct base the loss is over the 5% limit, so the gate must "
        "have closed -- this is the account that was blocked only by luck"
    )
    assert rm._is_trading_allowed is False, (
        "the daily loss is over the limit on the correct base, so the trading "
        "gate must be CLOSED"
    )

    # And the limit that actually fires must be the DAILY LOSS one. Asserted on
    # a separate mid-session object rather than on `rm` above: in the
    # restore-before-balance order the gate is already latched shut by the
    # min-balance check (the balance has not been fetched yet), and the
    # published reason is only refreshed on an allowed->blocked transition.
    # That latching is pre-existing behaviour and is not what this change is
    # about, so this pins the daily-loss computation on its own terms.
    mid_session = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    await mid_session.initialize_balance()
    mid_session.stats.today_pnl = -73.03
    mid_session._check_risk_limits()

    assert "daily loss" in mid_session.get_trading_disabled_reason().lower(), (
        "with today's opening balance and today's PnL the limit that breaches "
        "is the daily-loss limit, and the gate must name it"
    )


async def test_the_re_derived_base_is_the_one_persisted_for_the_next_start(
    redis_store,
):
    """The fix must be SELF-SUSTAINING, not a one-process patch.

    If the corrected base were not written back, the next restart would find
    the same stale base again and the defect would return on the following day.
    This is what makes the original defect indefinite: each restore re-stamped
    the stale base and saved it again.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_THREE,
        base=STALE_BASE,
        base_day_str=DAY_ONE,
    )
    rm = await _start_on_day(redis, redis_store, DAY_THREE, TRUE_BASE)
    await rm.save_state()

    written = json.loads(redis_store[_guard_key(rm)])
    assert written["start_of_day_balance"] == pytest.approx(TRUE_BASE), (
        "the stale base was written back, so it returns on the next restart"
    )
    assert written["start_of_day_day_str"] == DAY_THREE, (
        "the persisted base must carry the day it belongs to, or the next "
        "reader cannot tell whether it is current"
    )


# ---------------------------------------------------------------------------
# The invariant must hold in BOTH call orders, and on the same day
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["restore-first", "init-first"])
async def test_the_base_belongs_to_today_in_either_call_order(redis_store, order):
    """After any start, the base must be TODAY's opening balance.

    The controller uses "restore-first", but the two were previously coupled
    only through `guard_state_restored_today` skipping the re-baseline, so
    neither order was safe on its own.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_THREE,
        base=STALE_BASE,
        base_day_str=DAY_ONE,
    )
    rm = await _start_on_day(
        redis, redis_store, DAY_THREE, TRUE_BASE, order=order
    )

    assert rm.stats.start_of_day_balance == pytest.approx(TRUE_BASE)
    assert rm.stats.start_of_day_day_str == DAY_THREE


async def test_a_same_day_base_is_restored_not_re_derived(redis_store):
    """Positive control: the fix must not throw the good case away.

    A base captured TODAY is the real denominator and must survive a restart.
    Re-deriving it from the live balance would silently restate the day's loss
    against a mid-session balance, which is a different (and looser) quantity.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_THREE, balance=900.0)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_THREE,
        base=TRUE_BASE,
        base_day_str=DAY_THREE,
        today_pnl=-10.0,
    )
    rm = await _start_on_day(redis, redis_store, DAY_THREE, 900.0)

    assert rm.stats.start_of_day_balance == pytest.approx(
        TRUE_BASE
    ), "a base captured TODAY must be restored, not overwritten with the live balance"
    assert rm.stats.today_pnl == pytest.approx(-10.0), (
        "the day's PnL is day-scoped by day_str and must still be restored"
    )


async def test_a_previous_days_streak_is_still_discarded(redis_store):
    """The deliberate behaviour this change must NOT break.

    A payload from a PREVIOUS UTC day must still be discarded wholesale, so an
    account whose streak was legitimately cleared at the rollover is not
    re-blocked and a stale day-PnL is not carried into a new day. The argument
    in the code is correct and is preserved.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_TWO,
        base=STALE_BASE,
        base_day_str=DAY_TWO,
        today_pnl=-500.0,
    )

    restored = await probe.restore_state()

    assert restored is False, "a previous day's state must be discarded"
    assert probe.stats.consecutive_losses == 0
    assert probe.stats.today_pnl == 0.0, (
        "a stale day-PnL must not be carried into a new day"
    )


# ---------------------------------------------------------------------------
# Fail closed when the base is unknown -- and still trade on a first boot
# ---------------------------------------------------------------------------


async def test_an_unknown_base_fails_closed_instead_of_skipping_the_checks(
    redis_store,
):
    """The `> 1e-9` guard was a fail-OPEN, and it skipped BOTH checks.

    With no usable base the daily-loss and drawdown ratios cannot be computed,
    and "cannot be computed" is not "within limits". This asserts the gate
    closes and NAMES the cause instead of silently passing.
    """
    rm = _risk_manager(FakeRedis(redis_store), DAY_THREE, balance=10000.0)
    await rm.initialize_balance()
    # Simulate the base becoming unknown: the value is still there but it is
    # not known to belong to today.
    rm._daily_base_known = False
    rm.stats.start_of_day_balance = 0.0
    rm.stats.today_pnl = -500.0

    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "an unknown daily-loss base must fail CLOSED, not silently skip the "
        "drawdown and daily-loss checks and let the account trade"
    )
    reason = rm.get_trading_disabled_reason()
    assert BASE_UNKNOWN_REASON_FRAGMENT in reason, (
        f"the gate must name the unknown base as the cause, got {reason!r}"
    )


async def test_a_zero_base_known_to_be_todays_still_fails_closed(redis_store):
    """A base that is known-but-unusable is still UNKNOWN.

    `_daily_base_known` can be true while the value cannot divide anything, so
    the near-zero case needs its own fail-closed branch rather than falling
    through to the division.
    """
    rm = _risk_manager(FakeRedis(redis_store), DAY_THREE, balance=10000.0)
    await rm.initialize_balance()
    rm._daily_base_known = True
    rm.stats.start_of_day_balance = 0.0
    rm.stats.today_pnl = -500.0

    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a base of zero cannot produce a ratio, so the daily-loss limit is "
        "unknown and must fail closed rather than divide by zero"
    )
    assert BASE_UNKNOWN_REASON_FRAGMENT in rm.get_trading_disabled_reason()


async def test_a_positive_but_stale_day_base_still_fails_closed(redis_store):
    """A POSITIVE base can still be unusable, and this is the measured case.

    Mutation found this: a guard that only asks "is the base positive?" passes
    every zero-base test, because a zero base is rejected by both that guard
    and this one. The two differ on exactly one input -- a base that is
    positive but belongs to a day that has ended -- which is the production
    defect itself. So that input is pinned here explicitly: a stale-day base
    must NOT be used as a denominator, however large it is.
    """
    rm = _risk_manager(FakeRedis(redis_store), DAY_THREE, balance=TRUE_BASE)
    await rm.initialize_balance()
    # The restore refused to vouch for this base, and the base it left behind
    # is the one from an earlier day.
    rm._daily_base_known = False
    rm.stats.start_of_day_balance = STALE_BASE
    rm.stats.start_of_day_day_str = DAY_ONE
    rm.stats.today_pnl = -73.03

    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a base of $1270.59 captured on 2026-10-20 is positive, so a "
        "'is it positive?' guard would accept it -- and the daily loss would "
        "then read 5.75% instead of the true 8.07%. The gate must fail closed "
        "on a base that does not belong to the current UTC day, whatever its "
        "size"
    )
    assert BASE_UNKNOWN_REASON_FRAGMENT in rm.get_trading_disabled_reason(), (
        f"the gate must name the unknown base as the cause, got "
        f"{rm.get_trading_disabled_reason()!r}"
    )


async def test_a_first_boot_can_still_trade(redis_store):
    """The positive control for the two rules above.

    A brand-new account has no persisted state and no base. It must NOT be
    blocked: "fail closed on unknown" must not become "brick every new
    account". ``initialize_balance`` derives the base from the live balance,
    which is what makes the account tradable.
    """
    rm = _risk_manager(FakeRedis(redis_store), DAY_THREE, balance=10000.0)
    await rm.initialize_balance()

    restored = await rm.restore_state()

    assert restored is False, "nothing was persisted, so nothing was restored"
    assert rm.stats.start_of_day_balance == pytest.approx(10000.0)
    assert rm.stats.start_of_day_day_str == DAY_THREE
    assert rm._daily_base_known is True
    assert rm._is_trading_allowed is True, (
        "a first boot must be able to trade -- the fail-closed rule is about "
        "an UNKNOWN base, not about a missing one"
    )


async def test_an_account_within_its_limits_is_not_over_blocked(redis_store):
    """A correct base must not close the gate on a healthy account.

    The fix makes the gate stricter, so this pins that a small loss against a
    correctly re-derived base still trades.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_THREE,
        base=STALE_BASE,
        base_day_str=DAY_ONE,
        today_pnl=-1.00,
    )
    rm = await _start_on_day(redis, redis_store, DAY_THREE, TRUE_BASE)

    assert rm.stats.start_of_day_balance == pytest.approx(TRUE_BASE)
    assert rm._is_trading_allowed is True, (
        "a $1.00 loss against a correct base is 0.11% -- well inside the 5% "
        "limit -- so the account must still trade. If this fails the fix is "
        "over-blocking."
    )


# ---------------------------------------------------------------------------
# Known vs unknown: a missing key is not always a first boot
# ---------------------------------------------------------------------------


async def test_a_missing_key_for_an_account_that_traded_before_fails_closed(
    redis_store,
):
    """A lost guard state must not read as a fresh account.

    Previously a missing key returned "nothing was lost" with
    ``_guard_state_unverified = False``, so ``initialize_balance`` zeroed
    ``today_pnl`` and ``consecutive_losses`` and the gate re-opened. The
    "has traded before" marker is the discriminator: it is written under its
    own key and is never part of the payload, so it survives the loss of the
    thing it describes.
    """
    redis = FakeRedis(redis_store)
    rm = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    await rm.initialize_balance()
    await rm.save_state()

    # The account HAS traded (the marker is written), and now the guard state
    # itself is gone.
    assert f"{_guard_key(rm)}:seen" in redis_store, (
        "save_state must record that this controller has traded before"
    )
    del redis_store[_guard_key(rm)]

    restarted = await _start_on_day(redis, redis_store, DAY_THREE, TRUE_BASE)

    assert restarted._is_trading_allowed is False, (
        "the guard state is gone for an account that has traded before, so "
        "the streak and the day's PnL cannot be shown to be zero. Treating "
        "that as a first boot re-opens the gate on an unknown state"
    )
    assert STATE_LOST_REASON_FRAGMENT in restarted.get_trading_disabled_reason(), (
        f"the gate must say the state was LOST rather than merely unreadable, "
        f"got {restarted.get_trading_disabled_reason()!r}"
    )
    assert restarted._guard_state_lost is True


async def test_a_lost_state_heals_at_the_utc_rollover_without_an_operator(
    redis_store,
):
    """A lost state must not need a human to clear, and must not be permanent.

    It cannot heal on a re-read -- there is nothing left to read, so the
    Redis-unreachable latch's automatic release would keep re-opening the gate
    on an unknown streak. But a new UTC day starts the day's PnL and the loss
    streak at zero by definition, so the rollover is a sound, automatic release.
    """
    redis = FakeRedis(redis_store)
    rm = _risk_manager(redis, DAY_THREE, balance=TRUE_BASE)
    await rm.initialize_balance()
    await rm.save_state()
    del redis_store[_guard_key(rm)]

    restarted = await _start_on_day(redis, redis_store, DAY_THREE, TRUE_BASE)
    assert restarted._is_trading_allowed is False

    # The next UTC day.
    restarters = _risk_manager(redis, DAY_TWO, balance=TRUE_BASE)
    restarters._fake_day = "2026-10-23"
    await restarters.update_balance()

    assert restarters._guard_state_lost is False, (
        "the lost-state latch must release at the UTC rollover"
    )
    assert restarters._is_trading_allowed is True, (
        "on a new UTC day the PnL and the streak start at zero, so the account "
        "must be able to trade again without an operator clearing anything"
    )


async def test_a_missing_key_with_no_marker_is_still_a_first_boot(redis_store):
    """The discriminator must not brick a brand-new account.

    This is the counterweight to the test above: with no marker either, there
    has never been a guard state, so there is nothing lost and the account
    trades.
    """
    redis = FakeRedis(redis_store)
    rm = await _start_on_day(redis, redis_store, DAY_THREE, TRUE_BASE)

    assert rm._is_trading_allowed is True
    assert rm._guard_state_unverified is False
    assert rm.stats.start_of_day_balance == pytest.approx(TRUE_BASE)


async def test_an_unreadable_state_still_fails_closed(redis_store):
    """The pre-existing fail-closed rule must survive this change."""
    rm = _risk_manager(ExplodingRedis(redis_store), DAY_THREE, balance=10000.0)
    await rm.initialize_balance()

    await rm.restore_state()

    assert rm._guard_state_unverified is True
    assert rm._is_trading_allowed is False, (
        "an unreadable guard state must fail CLOSED, not open"
    )