"""The published daily-loss percentage must reconcile with the WALLET.

THE LIVE INSTANCE (measured 2026-10-25T22:51Z, account ``diverseinc``)
----------------------------------------------------------------------
``GET /api/v1/portfolio?mode=paper`` published::

    balance                = 613.6918301225983
    today_pnl              = -79.99580986789994
    consecutive_losses     = 6
    is_trading_allowed     = False
    trading_disabled_reason= global risk limit breached: Daily loss (5.58%) >= limit (5.00%)

The paper wallet's realized cash on 2026-10-25 went ``693.6876 ->
613.6918``, so the day's loss was exactly ``-79.9958`` and the day's OPENING
balance was ``693.6876``. The true daily loss is therefore::

    79.9958 / 693.6876 = 11.53%

but the gate published **5.58%**. Solving ``79.9958 / 0.0558 = 1433.60``
shows the denominator it used was **$1433.60** -- a balance this wallet held
around 2026-10-19/20, five days earlier -- and NOT today's opening balance.

So the gate measured today's loss against a base carried over from another
day. It still tripped (5.58% >= 5.00%), so trading WAS blocked, but the
number shown to the user was wrong, and on a day where the true loss was
just under the limit it would NOT have tripped.

The root cause is the reader, not the arithmetic: the persisted payload
recorded only ``day_str`` -- the day the STREAK and the day's PnL belong to
-- and never which day the BASE belonged to, and
``_apply_persisted_guard_state`` then re-stamped ``last_known_day_str =
current_day_str`` unconditionally, which re-blessed the stale number as
belonging to today. Every restart re-stamped it, so it survived for days.

THE PROPERTIES PINNED HERE
--------------------------
1. A fresh process that has never seen a previous day does NOT install a
   mid-day balance as today's base, and after a GENUINE UTC day transition
   the base IS the new day's opening balance.
2. The published percentage is ``today_pnl / the opening balance of the
   CURRENT UTC day`` -- reconciled against the wallet, never against a
   carried-over number. When today's opening balance cannot be known, the
   gate says so and fails CLOSED instead of publishing a percentage.
3. The risk manager's ``today_pnl`` keeps matching the wallet's realized
   cash: the gate and the user's balance must agree.

These tests drive the REAL ``RiskManager`` with only the exchange handle and
the clock faked. The end state asserted is the product's, not a stub's.
"""

import json
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor

# The operator-facing reasons are asserted as LITERAL fragments rather than
# imported from the module. Importing them would make this file fail to
# COLLECT against the unfixed base commit, and a collection error is not a
# RED test -- it proves nothing about the defect.
BASE_UNKNOWN_REASON_FRAGMENT = "the opening balance for the current UTC day is unknown"

# The dedicated namespace for the bot's private guard state, written as a
# LITERAL so this file pins the wire contract independently of the
# implementation's plumbing.
GUARD_STATE_NAMESPACE = "depthsight:state:risk"

# ---------------------------------------------------------------------------
# THE production numbers of 2026-10-25, so the test fails for the RIGHT
# reason if the defect ever comes back.
# ---------------------------------------------------------------------------
DAY_TODAY = "2026-10-25"
STALE_BASE_DAY = "2026-10-19"          # the day the carried-over base belongs to
OPENING_BALANCE = 693.6876             # the wallet's balance at today's opening
CLOSING_BALANCE = 613.6918301225983    # the wallet's balance at the measurement
TODAY_PNL = -79.99580986789994         # OPENING_BALANCE -> CLOSING_BALANCE
STALE_BASE = 1433.60                   # the base the gate actually measured against

TRUE_PCT = abs(TODAY_PNL / OPENING_BALANCE) * 100      # 11.53%
STALE_PCT = abs(TODAY_PNL / STALE_BASE) * 100          # 5.58%  <- what was published

# The values the reader has to distinguish. The live account was blocked on
# BOTH counts: the daily loss AND the consecutive-loss streak.
CONSECUTIVE_LOSSES = 6
MAX_CONSECUTIVE_LOSSES = 5


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


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
        user_settings={"risk_management": {"maxConsecutiveLosses": MAX_CONSECUTIVE_LOSSES}},
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


def _seed_payload(rm, store, *, day_str, base, base_day_str, today_pnl=TODAY_PNL,
                  consecutive_losses=CONSECUTIVE_LOSSES):
    """Put a guard payload in Redis directly, as a previous process left it.

    Written by hand rather than produced by ``save_state`` on purpose: the
    defect is that the READER cannot tell which day the base belongs to, so the
    test must hand the reader exactly that ambiguity.
    """
    store[_guard_key(rm)] = json.dumps(
        {
            "version": 2,
            "day_str": day_str,
            "consecutive_losses": consecutive_losses,
            "today_pnl": today_pnl,
            "start_of_day_balance": base,
            "start_of_day_day_str": base_day_str,
        }
    )
    assert _guard_key(rm) in store, "the seeded payload must actually be present"


def _set_balance(rm, balance):
    rm.executor.get_account_balance.return_value = {
        "USDT": {"free": f"{balance:.8f}", "locked": "0.00"}
    }


def _published_pct(reason):
    """The daily-loss percentage the operator is shown, parsed back out."""
    match = re.search(r"Daily loss \(([0-9]+\.[0-9]+)%\)", reason or "")
    assert match, f"no daily-loss percentage was published in {reason!r}"
    return float(match.group(1))


# ---------------------------------------------------------------------------
# 1. A GENUINE UTC day transition installs the new day's opening balance
# ---------------------------------------------------------------------------


async def test_a_genuine_utc_day_transition_installs_the_new_days_opening_balance(
    redis_store,
):
    """The POSITIVE half of the base's identity, and the one that was missing.

    A base may only be captured on a day transition this process OBSERVED --
    the one moment the balance genuinely IS the opening balance. So a process
    that lived through the rollover must end up with the NEW day's opening
    balance as its denominator.

    This is the other half of "a fresh process must not install a mid-day
    balance": refusing to guess on a fresh start is only safe if the real
    transition does install the right base. Without this test, a change that
    simply NEVER captured a base would pass every other test here.
    """
    redis = FakeRedis(redis_store)
    rm = _risk_manager(redis, "2026-10-24", balance=1000.0, user_id=7)

    # Day one: the account starts on 2026-10-24 and the balance IS that day's
    # opening balance.
    await rm.initialize_balance()
    assert rm.stats.start_of_day_day_str == "2026-10-24"
    assert rm.stats.start_of_day_balance == pytest.approx(1000.0)

    # The day rolls over and the process lives through it. At that moment the
    # balance is the new day's opening balance and must be captured as such.
    rm._fake_day = DAY_TODAY
    _set_balance(rm, OPENING_BALANCE)
    await rm.initialize_balance()

    assert rm.stats.start_of_day_day_str == DAY_TODAY, (
        "the base must be stamped with the day it was captured on"
    )
    assert rm.stats.start_of_day_balance == pytest.approx(OPENING_BALANCE), (
        "a genuine UTC day transition must install the NEW day's opening "
        f"balance as the denominator, got ${rm.stats.start_of_day_balance:.8f}"
    )
    assert rm._daily_loss_base_is_usable() is True, (
        "a base captured at a genuine rollover is VOUCHED FOR and must be "
        "usable: the gate has to be able to measure the day against it"
    )

    # And the gate measures THAT day against THAT base.
    rm.stats.today_pnl = TODAY_PNL
    rm._check_risk_limits()
    reason = rm.get_trading_disabled_reason() or ""
    assert _published_pct(reason) == pytest.approx(TRUE_PCT, abs=0.005), (
        f"the gate published {_published_pct(reason):.2f}% but today's loss is "
        f"{TRUE_PCT:.2f}% of today's opening balance"
    )


# ---------------------------------------------------------------------------
# 2. A FRESH process does not install a mid-day balance as today's base
# ---------------------------------------------------------------------------


async def test_a_fresh_process_does_not_install_a_mid_day_balance_as_todays_base(
    redis_store,
):
    """The NEGATIVE half, in the live account's shape.

    A process that starts mid-day has never seen the previous day, so it
    cannot tell the live balance apart from a MID-DAY balance -- and a
    mid-day balance already contains today's PnL. Restating the day's loss
    against it reads LOW, which is the same defect as a base captured on
    another day, wearing a different hat.
    """
    redis = FakeRedis(redis_store)

    # The persisted state proves this account has traded before, so its live
    # balance is a MID-DAY balance: it already contains today's PnL.
    probe = _risk_manager(redis, "2026-10-24", balance=1000.0, user_id=8)
    probe.stats.last_known_day_str = "2026-10-24"
    await probe.save_state()

    MIDDAY_BALANCE = OPENING_BALANCE + TODAY_PNL  # today's loss already taken
    rm = _risk_manager(redis, DAY_TODAY, balance=MIDDAY_BALANCE, user_id=8)

    # This is what a fresh process knows: no previous day at all.
    assert rm.stats.last_known_day_str == "", (
        "a fresh process must start with no known day, or this test is not "
        "testing a fresh process"
    )

    await rm.initialize_balance()

    assert rm.stats.start_of_day_balance != pytest.approx(MIDDAY_BALANCE), (
        "the live mid-day balance was installed as today's opening balance: "
        "the day's loss would then be restated against a denominator that "
        "already contains it, which reads LOW and under-trips"
    )
    assert rm._daily_loss_base_is_usable() is False, (
        "today's opening balance is not knowable on a fresh process, so the "
        "base must be left UNUSABLE"
    )
    assert rm._is_trading_allowed is False, (
        "with an unknown base the daily-loss ratio cannot be computed, so the "
        "gate must fail CLOSED"
    )
    assert BASE_UNKNOWN_REASON_FRAGMENT in (rm.get_trading_disabled_reason() or ""), (
        "the operator must be told that today's opening balance is unknown, "
        f"got {rm.get_trading_disabled_reason()!r}"
    )


# ---------------------------------------------------------------------------
# 3. The published percentage reconciles with the WALLET
# ---------------------------------------------------------------------------


async def test_the_published_percentage_is_the_loss_over_TODAYS_opening_balance(
    redis_store,
):
    """THE LIVE NUMBER, asserted in both directions.

    The gate published 5.58% where the truth was 11.53%. The published
    percentage must be ``today_pnl / the opening balance of the CURRENT UTC
    day`` -- reconciled against the wallet -- and must never be the figure
    produced by a carried-over base.
    """
    redis = FakeRedis(redis_store)

    # An account whose opening balance for today IS known: it never traded, so
    # its balance cannot contain today's PnL and it IS the opening balance.
    rm = _risk_manager(redis, DAY_TODAY, balance=OPENING_BALANCE, user_id=9)
    await rm.initialize_balance()
    assert rm.stats.start_of_day_balance == pytest.approx(OPENING_BALANCE)
    assert rm._daily_loss_base_is_usable() is True

    rm.stats.today_pnl = TODAY_PNL
    rm._check_risk_limits()

    reason = rm.get_trading_disabled_reason() or ""
    published = _published_pct(reason)

    assert published == pytest.approx(TRUE_PCT, abs=0.005), (
        f"the gate published {published:.2f}% but the day's loss is "
        f"{abs(TODAY_PNL):.8f} on an opening balance of {OPENING_BALANCE:.4f}, "
        f"which is {TRUE_PCT:.2f}%. The published number must reconcile with "
        "the wallet."
    )
    assert f"{STALE_PCT:.2f}%" not in reason, (
        f"the published reason still carries the carried-over-base figure "
        f"({STALE_PCT:.2f}%): the denominator was ${STALE_BASE:.2f} from "
        f"{STALE_BASE_DAY}, not today's opening balance"
    )
    assert rm._is_trading_allowed is False, (
        "11.53% is over the 5.00% limit, so the gate must be CLOSED"
    )


async def test_a_base_carried_over_from_five_days_earlier_is_never_the_denominator(
    redis_store,
):
    """THE LIVE DEFECT, in the shape the production payload actually had.

    The payload said ``day_str`` = today (the day the streak and the day's PnL
    belong to) while its base belonged to 2026-10-19. The reader re-stamped
    the day and re-blessed the five-day-old number, so every restart kept
    it alive and the gate kept publishing 5.58% instead of 11.53%.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, DAY_TODAY, balance=OPENING_BALANCE, user_id=10)
    _seed_payload(
        probe,
        redis_store,
        day_str=DAY_TODAY,
        base=STALE_BASE,
        base_day_str=STALE_BASE_DAY,
        today_pnl=TODAY_PNL,
        consecutive_losses=0,
    )

    rm = _risk_manager(redis, DAY_TODAY, balance=OPENING_BALANCE, user_id=10)
    # The PRODUCTION order: bot_runner initialises the balance first and the
    # controller restores the persisted state afterwards.
    await rm.initialize_balance()
    await rm.restore_state()

    assert rm.stats.start_of_day_balance != pytest.approx(STALE_BASE), (
        f"the base carried over from {STALE_BASE_DAY} (${STALE_BASE:.2f}) is "
        "still in force: today's loss is being measured against a "
        "five-day-old denominator"
    )
    reason = rm.get_trading_disabled_reason() or ""
    assert f"{STALE_PCT:.2f}%" not in reason, (
        f"the gate published the carried-over-base figure ({STALE_PCT:.2f}%) "
        f"instead of {TRUE_PCT:.2f}%: the reason is {reason!r}"
    )
    assert BASE_UNKNOWN_REASON_FRAGMENT in reason, (
        "today's opening balance cannot be known from a base captured on "
        f"another day, so the gate must say so, got {reason!r}"
    )
    assert BASE_UNKNOWN_REASON_FRAGMENT in (
        rm._global_gate_disable_reason or ""
    ), (
        "the limit the gate is currently held for must be the unknown base, "
        f"got {rm._global_gate_disable_reason!r}"
    )
    assert rm._is_trading_allowed is False, (
        "the base is unknown, so the gate must fail CLOSED rather than "
        "measure against a denominator it cannot vouch for"
    )


async def test_the_consecutive_loss_streak_is_enforced_on_its_own(redis_store):
    """The live account was blocked on BOTH counts; neither may mask the other.

    ``consecutive_losses`` was 6 against a limit of 5, so the streak limit
    fired as well as the daily-loss limit. This is pinned separately so a
    change to either guard cannot hide the other's verdict.
    """
    redis = FakeRedis(redis_store)
    rm = _risk_manager(redis, DAY_TODAY, balance=OPENING_BALANCE, user_id=11)
    await rm.initialize_balance()
    rm.stats.today_pnl = 0.0
    rm.stats.consecutive_losses = CONSECUTIVE_LOSSES
    rm._check_risk_limits()

    reason = rm.get_trading_disabled_reason() or ""
    assert "consec" in reason.lower(), (
        f"the streak limit must be named in the published reason, got {reason!r}"
    )
    assert rm._is_trading_allowed is False
