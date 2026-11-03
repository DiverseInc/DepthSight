"""The PUBLISHED risk reason must describe the limit that is in force NOW.

THE DEFECT
----------
``_check_risk_limits()`` records TWO reasons for the same event:

* ``_global_gate_disable_reason`` -- WHICH limit is in force. It was
  refreshed on EVERY evaluation.
* ``_trading_disabled_reason`` -- the OPERATOR-VISIBLE reason, returned by
  ``get_trading_disabled_reason()`` and published to the API. It was written
  ONLY on the allowed -> blocked TRANSITION, so it froze on the FIRST breach
  that ever tripped.

Once the peak-to-trough drawdown limit started tripping EARLIER than the
daily-loss limit, the freeze became reachable in a scenario that used to
publish the daily loss: the gate closed on the DRAWDOWN at the UTC rollover,
and when the day's loss was measured afterwards the published reason still
named the drawdown -- with a stale percentage and a stale trough.

THE PROPERTIES PINNED HERE
--------------------------
1. A LATER evaluation refreshes the published reason: it names the limit that
   is in force NOW, with the number measured NOW.
2. The emergency-stop latch still OWNS the published reason while it is
   engaged. A limit reason must not overwrite "EMERGENCY STOP active (...)".
   The breach itself is still recorded in ``_global_gate_disable_reason``, so
   releasing the stop cannot hide it.
3. The published reason and ``_global_gate_disable_reason`` never disagree.

These tests drive the REAL ``RiskManager`` with only the exchange handle and
the clock faked. The end state asserted is the product's, not a stub's.
"""

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor

# ---------------------------------------------------------------------------
# THE production numbers, so the test fails for the RIGHT reason if the defect
# ever comes back.
# ---------------------------------------------------------------------------

# Day one: the account opens the day at $1000.00. That balance becomes the
# HIGH-WATER MARK, which is a server-owned quantity and is NOT reset at the
# UTC rollover.
DAY_ONE = "2026-10-24"
PEAK = 1000.00

# Day two: the balance falls to $693.6876, which IS the new day's opening
# balance, captured at the rollover.
DAY_TWO = "2026-10-25"
OPENING_BALANCE = 693.6876

# The day's realised loss, measured against the new day's opening balance.
TODAY_PNL = -79.99580986789994

# The drawdown at the rollover, against the $1000.00 high-water mark.
FIRST_DRAWDOWN_PCT = (PEAK - OPENING_BALANCE) / PEAK * 100.0        # 30.63%
# The day's loss, against the new day's opening balance.
TRUE_DAILY_LOSS_PCT = abs(TODAY_PNL / OPENING_BALANCE) * 100.0      # 11.53%
# A second, smaller loss, used to prove the published number MOVES AGAIN.
SECOND_PNL = -40.0
SECOND_DAILY_LOSS_PCT = abs(SECOND_PNL / OPENING_BALANCE) * 100.0   # 5.77%

# The production limits this test depends on. Asserted as preconditions in
# the harness below: if a limit is ever loosened, this file must fail loudly
# rather than quietly stop exercising the breach.
DRAWDOWN_LIMIT_PCT = 20.0
DAILY_LOSS_LIMIT_PCT = 5.0


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses."""

    def __init__(self, store=None):
        self.store = {} if store is None else store

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


def _risk_manager(redis_client, day, balance=10000.0, user_id=1):
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
        user_settings={"risk_management": {}},
        api_key_name="paper",
    )
    rm.redis_client = redis_client
    rm.executor = executor
    rm._fake_day = day
    rm._get_current_day_start_info = lambda: (
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
        rm._fake_day,
    )
    return rm


def _set_balance(rm, balance):
    rm.executor.get_account_balance.return_value = {
        "USDT": {"free": f"{balance:.8f}", "locked": "0.00"}
    }


async def _account_stopped_on_the_drawdown(redis, user_id):
    """An account whose gate closed on the DRAWDOWN at the UTC rollover.

    This is the production shape: the peak-to-trough limit trips at the
    rollover (the balance fell 30.63% from the high-water mark), which closes
    the gate BEFORE the day's loss has been measured against the new day's
    opening balance.
    """
    rm = _risk_manager(redis, DAY_ONE, balance=PEAK, user_id=user_id)

    # The limits this scenario depends on. Asserted, not assumed: a loosened
    # limit must fail this file loudly instead of silently skipping the
    # breach.
    assert rm.max_drawdown_threshold == pytest.approx(DRAWDOWN_LIMIT_PCT / 100.0)
    assert rm.daily_max_loss_threshold == pytest.approx(DAILY_LOSS_LIMIT_PCT / 100.0)

    # Day one: the balance IS that day's opening balance.
    await rm.initialize_balance()
    assert rm.stats.start_of_day_balance == pytest.approx(PEAK)
    assert rm.stats.consecutive_losses == 0, (
        "the loss streak must stay out of this scenario: the streak limit has "
        "its own reason and would mask the one under test"
    )

    # The day rolls over and the process lives through it.
    rm._fake_day = DAY_TWO
    _set_balance(rm, OPENING_BALANCE)
    await rm.initialize_balance()

    assert rm.stats.start_of_day_balance == pytest.approx(OPENING_BALANCE), (
        "precondition: the new day's opening balance was captured at the "
        "rollover, so the daily loss can be measured against it"
    )
    assert rm._daily_loss_base_is_usable() is True
    assert rm._is_trading_allowed is False, (
        "precondition: the drawdown limit closed the gate at the rollover"
    )
    return rm


def _published(reason):
    """The daily-loss percentage the operator is shown, parsed back out."""
    match = re.search(r"Daily loss \(([0-9]+\.[0-9]+)%\)", reason or "")
    if not match:
        return None
    return float(match.group(1))


# ---------------------------------------------------------------------------
# 1. A LATER evaluation refreshes the published reason
# ---------------------------------------------------------------------------


async def test_the_published_reason_names_the_limit_in_force_now_not_the_first_that_tripped(
    redis_store,
):
    """THE DEFECT.

    The gate closed on the DRAWDOWN at the rollover, so the published reason
    named the drawdown. A later evaluation measured the day's loss against the
    new day's opening balance and found THAT limit in force. The published
    reason has to follow, or the operator is told about a limit that is no
    longer the one holding the gate.
    """
    redis = FakeRedis(redis_store)
    rm = await _account_stopped_on_the_drawdown(redis, user_id=101)

    # The first breach: the drawdown, at the rollover.
    first = rm.get_trading_disabled_reason() or ""
    assert "Max drawdown" in first, (
        f"precondition: the drawdown limit must be what closed the gate, got "
        f"{first!r}"
    )

    # A LATER evaluation. The day's loss is now measured against the new
    # day's opening balance, and the daily-loss limit is the one in force.
    rm.stats.today_pnl = TODAY_PNL
    rm._check_risk_limits()

    published = rm.get_trading_disabled_reason() or ""
    assert "Daily loss" in published, (
        "the published reason must name the limit that is in force NOW, but "
        f"it still names the first one that tripped: {published!r}"
    )
    assert "Max drawdown" not in published, (
        "the published reason still names a limit that is no longer the one in "
        f"force: {published!r}"
    )
    assert published == rm._global_gate_disable_reason, (
        "the two reasons must never disagree: the published one is "
        f"{published!r} and the gate's own is {rm._global_gate_disable_reason!r}"
    )
    assert rm._is_trading_allowed is False, (
        "refreshing the reason must NOT re-open the gate"
    )


# ---------------------------------------------------------------------------
# 2. The emergency stop still owns the published reason
# ---------------------------------------------------------------------------


async def test_the_emergency_stop_still_owns_the_published_reason_while_a_limit_is_in_force(
    redis_store,
):
    """The latch OWNS the published reason. A limit must not overwrite it.

    The emergency stop is the reason the operator must see first: it is the
    thing they have to clear. A limit reason written over it would hide the
    latch.

    The breach is still RECORDED in ``_global_gate_disable_reason``, so
    releasing the latch cannot silently re-open the gate past a tripped limit.
    """
    redis = FakeRedis(redis_store)
    rm = await _account_stopped_on_the_drawdown(redis, user_id=102)

    rm.trigger_emergency_stop("operator pulled the kill switch")
    assert "EMERGENCY STOP active" in (rm.get_trading_disabled_reason() or ""), (
        "precondition: engaging the latch must publish the stop message"
    )

    # A limit is in force at the same time as the latch.
    rm.stats.today_pnl = TODAY_PNL
    rm._check_risk_limits()

    published = rm.get_trading_disabled_reason() or ""
    assert "EMERGENCY STOP" in published, (
        "the emergency stop owns the published reason: a limit reason must "
        f"not overwrite it, got {published!r}"
    )
    assert "Daily loss" not in published, (
        f"a limit reason overwrote the emergency stop message: {published!r}"
    )

    # ...but the breach is not hidden: the latch's release still sees it.
    assert "Daily loss" in (rm._global_gate_disable_reason or ""), (
        "the limit in force must still be recorded, or releasing the latch "
        "would re-open the gate past a tripped limit: "
        f"{rm._global_gate_disable_reason!r}"
    )
    assert rm._is_trading_allowed is False


# ---------------------------------------------------------------------------
# 3. The published reason carries the CURRENT measured percentage
# ---------------------------------------------------------------------------


async def test_the_published_reason_carries_the_current_measured_percentage(
    redis_store,
):
    """The number in the published reason is the number measured NOW.

    Two refreshes are exercised: the drawdown's percentage when the day's
    loss is measured, and the daily loss's own percentage when the day's loss
    changes. A stale figure left in the published reason is a wrong number in
    front of the operator, which is the whole point of this fix.
    """
    redis = FakeRedis(redis_store)
    rm = await _account_stopped_on_the_drawdown(redis, user_id=103)

    stale = rm.get_trading_disabled_reason() or ""
    assert f"{FIRST_DRAWDOWN_PCT:.2f}%" in stale, (
        f"precondition: the first breach must publish the drawdown "
        f"percentage, got {stale!r}"
    )

    # Refresh #1: the day's loss is measured against the new day's opening
    # balance and the daily-loss limit is the one in force.
    rm.stats.today_pnl = TODAY_PNL
    rm._check_risk_limits()

    published = rm.get_trading_disabled_reason() or ""
    assert _published(published) == pytest.approx(TRUE_DAILY_LOSS_PCT, abs=0.005), (
        f"the published reason must carry the daily loss measured NOW "
        f"({TRUE_DAILY_LOSS_PCT:.2f}%), got {published!r}"
    )
    assert f"{FIRST_DRAWDOWN_PCT:.2f}%" not in published, (
        f"the published reason still carries the STALE drawdown percentage "
        f"({FIRST_DRAWDOWN_PCT:.2f}%): {published!r}"
    )

    # Refresh #2: a different loss for the same day. The published number
    # has to move AGAIN -- it tracks the evaluation, not the first one.
    rm.stats.today_pnl = SECOND_PNL
    rm._check_risk_limits()

    published = rm.get_trading_disabled_reason() or ""
    assert _published(published) == pytest.approx(SECOND_DAILY_LOSS_PCT, abs=0.005), (
        f"the published reason must carry the daily loss measured NOW "
        f"({SECOND_DAILY_LOSS_PCT:.2f}%), got {published!r}"
    )
    assert f"{TRUE_DAILY_LOSS_PCT:.2f}%" not in published, (
        f"the published reason still carries the PREVIOUS day's loss "
        f"({TRUE_DAILY_LOSS_PCT:.2f}%): {published!r}"
    )
    assert rm._is_trading_allowed is False, (
        "both losses are over the limit, so the gate must stay closed"
    )


# ---------------------------------------------------------------------------
# 4. The symbol gate still publishes the current global reason
# ---------------------------------------------------------------------------


async def test_the_symbol_gate_still_publishes_the_current_global_reason(
    redis_store,
):
    """``is_symbol_trading_allowed()`` keeps refreshing the published reason.

    That path refreshes the published reason from
    ``_global_gate_disable_reason`` when the global gate is closed. It must
    keep working, and it must keep publishing the CURRENT limit.
    """
    redis = FakeRedis(redis_store)
    rm = await _account_stopped_on_the_drawdown(redis, user_id=104)

    rm.stats.today_pnl = TODAY_PNL
    rm._check_risk_limits()

    assert await rm.is_symbol_trading_allowed("BTCUSDT") is False, (
        "precondition: the global gate is closed, so no symbol may trade"
    )
    published = rm.get_trading_disabled_reason() or ""
    assert "Daily loss" in published, (
        f"the symbol gate must publish the limit in force, got {published!r}"
    )
    assert published == rm._global_gate_disable_reason
