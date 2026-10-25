"""THE F3 REGRESSION: a PREVIOUS-day payload must leave the opening base UNKNOWN.

THE DEFECT (measured by the review panel at commit f9af7af)
-----------------------------------------------------------
The daily-loss gate at ``risk_manager.py`` is a RATIO::

    daily_loss_pct = abs(today_pnl / start_of_day_balance)

``start_of_day_balance`` is the day's OPENING balance. It is only
meaningful if it is the balance at the START of the day -- and on a fresh
process the live balance is a MID-DAY balance that already contains today's
PnL. Installing it as the opening base measures the day's loss against a
denominator that contains it, so the gate reads LOW and under-trips.

The panel measured exactly that, with a previous-day payload in Redis::

    start_of_day_balance = 865.23   stamp='2026-10-22'   (the MID-DAY balance)
    usable=True   trading ALLOWED=True   today_pnl=0.0
    gate reads daily loss = 0.0000%   TRUE daily loss = 4.4188%
    PERSISTED: day_str='2026-10-22', start_of_day_balance=865.23

A 0.0000% reading against a true 4.4188% loss, with trading ALLOWED, is the
same defect class this whole branch exists to kill -- reintroduced through a
different door.

TWO CAUSES, and the test below pins BOTH:

* ``_check_and_reset_daily_stats`` treated an EMPTY ``last_known_day_str``
  ("this process has no known day yet") as "the day changed", so the FIRST
  observation of a wallet installed the mid-day balance as today's base.
* a payload whose stored day is a PREVIOUS day returned early from
  ``_apply_persisted_guard_state`` without invalidating a base that was
  already stamped TODAY, so the mid-day base survived the restore.

THE BLIND SPOT THIS FILE CLOSES
-------------------------------
``tests/test_daily_loss_base_is_current_day.py`` seeds a payload whose
``day_str`` is TODAY. The PREVIOUS-day path -- the one the panel measured --
was covered by no test at all. That is why it survived.

These tests drive the REAL ``RiskManager`` with only the exchange handle and
the clock faked. The end state asserted is the product's, not a stub's.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module.exchanges import ExchangeExecutor
from bot_module.risk_manager import RiskManager

# The operator-facing reason is asserted as a LITERAL fragment rather than
# imported from the module: importing it would make this file fail to COLLECT
# against the unfixed base commit, and a collection error is not a RED test.
BASE_UNKNOWN_REASON_FRAGMENT = "the opening balance for the current UTC day is unknown"

# The dedicated namespace for the bot's private guard state, written as a
# LITERAL so this file pins the wire contract independently of the
# implementation's plumbing.
GUARD_STATE_NAMESPACE = "depthsight:state:risk"

# The panel's numbers, so the test fails for the RIGHT reason if the defect
# ever comes back.
MID_DAY_BALANCE = 865.23      # the live balance: it ALREADY contains today's loss
TRUE_OPENING_BALANCE = 905.23  # the balance at the start of the day
TODAY = "2026-10-22"
YESTERDAY = "2026-10-21"

# The loss the gate MUST see once the true base is known: 4.4188% of the
# opening balance. The defect made it read 0.0000%.
TRUE_LOSS_PCT = (
    abs((TRUE_OPENING_BALANCE - MID_DAY_BALANCE) / TRUE_OPENING_BALANCE) * 100.0
)


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses.

    The store is a dict and is deliberately SHARED between the RiskManagers
    below: handing the same store to a second RiskManager is exactly what
    "the bot restarted" means here.
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


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


def _risk_manager(redis_client, day, balance=10000.0, user_id=1, api_key_id=None):
    """A real RiskManager with only the exchange handle and the clock faked.

    The clock is faked by overriding ``_get_current_day_start_info``, which
    every day-sensitive path funnels through.
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


def _seed_payload(rm, store, *, day_str, base, base_day_str, today_pnl=0.0):
    """Put a guard payload in Redis directly, as a previous process left it.

    Written by hand rather than produced by ``save_state`` on purpose: the
    defect is in the READER, so the test must hand the reader exactly the
    ambiguity that breaks it.
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


def _seed_previous_day_payload(rm, store):
    """The panel's scenario: yesterday's state, today's mid-day balance."""
    _seed_payload(
        rm,
        store,
        day_str=YESTERDAY,
        base=TRUE_OPENING_BALANCE,
        base_day_str=YESTERDAY,
        today_pnl=0.0,
    )


async def _start_on_day(redis, store, day, balance, *, order="restore-first"):
    """Run one process start for `day`, in the controller's call order.

    The controller calls ``restore_state()`` and then ``initialize_balance()``,
    which is "restore-first". "init-first" is pinned too, because the
    invariant has to hold in BOTH orders.
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
# THE regression: a previous-day payload must not leave a base in force
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["restore-first", "init-first"])
async def test_a_previous_day_payload_leaves_the_base_unknown_and_the_gate_closed(
    redis_store, order
):
    """A payload from YESTERDAY must be treated as base-UNKNOWN.

    Yesterday's state says nothing about today's opening balance, and the
    live balance is a MID-DAY balance that already contains today's loss.
    So the base must stay UNKNOWN and the daily-loss gate must fail CLOSED
    -- in BOTH controller call orders.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, TODAY, balance=MID_DAY_BALANCE)
    _seed_previous_day_payload(probe, redis_store)

    rm = await _start_on_day(
        redis, redis_store, TODAY, MID_DAY_BALANCE, order=order
    )

    assert rm.stats.start_of_day_balance != pytest.approx(MID_DAY_BALANCE), (
        f"the mid-day balance {MID_DAY_BALANCE} was installed as today's "
        "opening balance. It already contains today's loss, so the gate "
        f"measures {abs((MID_DAY_BALANCE - MID_DAY_BALANCE) / MID_DAY_BALANCE) * 100:.4f}% "
        f"instead of the true {TRUE_LOSS_PCT:.4f}% -- the gate reads LOW and "
        "under-trips"
    )
    assert rm.stats.start_of_day_day_str != TODAY, (
        "a base was stamped with TODAY's day key, but nothing observed a UTC "
        "day transition today: a previous-day payload proves the account has "
        "traded, so its live balance is a mid-day balance and cannot be "
        "today's opening balance"
    )
    assert rm._daily_loss_base_is_usable() is False, (
        "the opening balance must be UNKNOWN, not usable: the gate may never "
        "measure the day's loss against a denominator it cannot vouch for"
    )
    assert rm._is_trading_allowed is False, (
        "the gate must be CLOSED while the opening balance is unknown"
    )
    reason = rm.get_trading_disabled_reason() or ""
    assert BASE_UNKNOWN_REASON_FRAGMENT in reason, (
        f"the gate must name the unknown base as the cause, got {reason!r}"
    )


@pytest.mark.asyncio
async def test_the_wrong_base_is_not_persisted_with_todays_stamp(redis_store):
    """The wrong base must not survive into the next process either.

    The panel measured the mid-day base being PERSISTED with today's stamp,
    which is how a mid-day base outlives the process that installed it.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, TODAY, balance=MID_DAY_BALANCE)
    _seed_previous_day_payload(probe, redis_store)

    rm = await _start_on_day(
        redis, redis_store, TODAY, MID_DAY_BALANCE, order="restore-first"
    )
    await rm.save_state()

    written = json.loads(redis_store[_guard_key(rm)])
    assert float(written.get("start_of_day_balance") or 0.0) != pytest.approx(
        MID_DAY_BALANCE
    ), (
        f"the mid-day balance {MID_DAY_BALANCE} was persisted as the opening "
        "balance: the next process would start with a denominator that "
        "already contains today's loss"
    )
    assert written.get("start_of_day_day_str") != TODAY, (
        "a base was persisted with TODAY's day key even though no UTC day "
        "transition was observed"
    )


@pytest.mark.asyncio
async def test_a_previous_day_payload_invalidates_a_base_already_stamped_today(
    redis_store,
):
    """A previous-day payload must not LEAVE a today-stamped base in force.

    The restore path returns early for a previous-day payload. If a base is
    already stamped with today's day key when it gets there, that base must
    be invalidated: the payload proves the account has traded, so the base
    cannot be today's opening balance.
    """
    redis = FakeRedis(redis_store)
    probe = _risk_manager(redis, TODAY, balance=MID_DAY_BALANCE)
    _seed_previous_day_payload(probe, redis_store)

    rm = _risk_manager(redis, TODAY, balance=MID_DAY_BALANCE)
    # A base that is already stamped today -- e.g. captured at a genuine UTC
    # rollover before a stale payload is read back.
    rm.stats.start_of_day_balance = MID_DAY_BALANCE
    rm.stats.start_of_day_day_str = TODAY

    await rm.restore_state()

    assert rm._daily_loss_base_is_usable() is False, (
        "a previous-day payload must invalidate a base that is stamped today: "
        "the base cannot be today's opening balance"
    )
    assert rm.stats.start_of_day_day_str != TODAY, (
        "the invalidated base kept today's day key"
    )
