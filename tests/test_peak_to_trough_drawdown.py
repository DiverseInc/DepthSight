"""The `maxDrawdown` limit is a PEAK-TO-TROUGH limit.

THE DEFECT
----------
``bot_module/risk_manager.py`` computed the drawdown as::

    drawdown_pct = abs(today_pnl / start_of_day_balance) if today_pnl < 0 else 0

which is the SAME expression as the daily-loss check one line below it, and
both were gated with ``>=``. The daily-loss limit (5%) is always the tighter
of the two, so the 10% drawdown limit could never be the limit that fired:
the account's high-water mark was never consulted at all.

It is worse than a duplicate check. The expression is ``0`` whenever
``today_pnl`` is POSITIVE, so an account that had fallen 12% below its
high-water mark but happened to make money TODAY measured 0% and went on
trading.

THE BACKTESTERS ALREADY DID IT RIGHT, and they are the precedent:
``bot_module/depthsight_backtester.py``::

    drawdown = (self.stats["peak_equity"] - self.current_balance) / self.stats["peak_equity"]
    self.stats["peak_equity"] = max(self.stats["peak_equity"], self.current_balance)

so the live gate and the backtest disagreed about the same limit. These
tests pin the live bot to the backtesters' semantics.

THE PEAK IS SERVER-OWNED
------------------------
The peak is NOT a client-writable setting: it is not a field of
``RiskManagementSettings``, it is not in ``SERVER_RISK_FLOOR`` and it is not
in ``RISK_FIELD_TO_RM_KEY``. Every field in those IS client-writable through
``api/routes/config.py``'s ``_the_fields_the_client_actually_sent``, so a
client-writable peak would let a client declare its own high-water mark and
loosen its own limit.

It is persisted with the rest of the trading-guard state (the same Redis key,
scoped per user AND per api key) because the bot restarts on every deploy,
and it fails CLOSED when it cannot be read.

THE WINDOW
----------
The peak expires after ``DRAWDOWN_PEAK_WINDOW_DAYS`` (7 days, chosen by the
owner on 2026-11-01) and is then re-established from the balance at that
moment. The peak's own timestamp travels with it through the persisted
payload, so the window survives a restart too. It is NOT a daily reset: a
daily reset would collapse the peak-to-trough limit back into the daily-loss
limit and change nothing.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module import risk_manager as rm_module
from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor


# The dedicated namespace for the bot's private guard state, written as a
# LITERAL rather than read from ``config`` so that this file pins the wire
# contract independently of the implementation's plumbing.
GUARD_STATE_NAMESPACE = "depthsight:state:risk"

# The operator-facing reason is asserted as a LITERAL fragment rather than
# imported from the module. Importing it would make this file fail to
# COLLECT against the unfixed base commit, and a collection error is not a
# RED test -- it proves nothing about the defect.
PEAK_UNKNOWN_REASON_FRAGMENT = "high-water mark"

# The window the owner chose on 2026-11-01. Asserted as a literal so that
# the test fails with a readable message (and not a NameError) if the
# constant is missing or renamed.
OWNER_CHOSEN_PEAK_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses.

    The store is a dict, and it is deliberately SHARED between the
    RiskManagers below: handing the same store to a second RiskManager is
    exactly what "the bot restarted" means here. A fake that kept its state
    inside the RiskManager could not express a restart at all.
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


def _risk_manager(
    redis_client,
    *,
    user_id=1,
    api_key_id=None,
    balance=10000.0,
    max_drawdown=10.0,
    daily_loss=5.0,
):
    """A real RiskManager with only the exchange handle faked.

    The ``risk_management`` dict is COMPLETE: every field the client sends
    is present, so the settings this test exercises are the settings the
    product really reads.
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
        user_settings={
            "risk_management": {
                "maxDrawdown": max_drawdown,
                "maxConsecutiveLosses": 5,
                "dailyMaxLossPercent": daily_loss,
                "maxConcurrentTrades": 5,
                "stopLossEnabled": True,
                "defaultStopLossPercent": 1.0,
                "maxStopDistancePct": 5.0,
                "riskPerTradePercent": 1.0,
            }
        },
        api_key_name="paper",
    )
    # The RiskManager builds its own Redis client in __init__; swap in the
    # fake so the test can observe exactly what was written.
    rm.redis_client = redis_client
    # The controller hands the api key id over after the RiskManager is
    # built (the RiskManager is constructed first).
    rm.api_key_id = api_key_id
    return rm


def _guard_key(rm):
    return f"{GUARD_STATE_NAMESPACE}:{rm.user_id}:{rm.api_key_id}"


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _peak_of(rm):
    """The account's high-water mark, or None when it is not set at all."""
    return getattr(rm, "_peak_equity", None)


def _peak_set_at(rm):
    return getattr(rm, "_peak_equity_set_at", None)


def _set_todays_opening_balance(rm, balance):
    """Install today's opening balance AND stamp it with today.

    Without the stamp the base is not usable and the daily-loss ratio
    cannot be computed at all, which is a different test.
    """
    rm.stats.start_of_day_balance = float(balance)
    rm.stats.start_of_day_day_str = rm._current_utc_day_str()


# ---------------------------------------------------------------------------
# 1. The peak survives a restart -- restored, NOT re-derived
# ---------------------------------------------------------------------------


async def test_the_peak_survives_a_restart(redis_store):
    """A restart must not forget the high-water mark.

    The bot restarts on every deploy. A peak that is re-derived from the
    live balance on every start is a peak that forgets, and a forgotten
    peak means the drawdown limit is measured against whatever the balance
    happens to be today.
    """
    rm_a = _risk_manager(FakeRedis(redis_store), balance=12000.0)
    await rm_a.initialize_balance()
    peak_at = _peak_set_at(rm_a)
    assert _peak_of(rm_a) == pytest.approx(12000.0), (
        "the account's first observed balance must become its high-water "
        f"mark, got {_peak_of(rm_a)!r}"
    )
    await rm_a.save_state()

    payload = json.loads(redis_store[_guard_key(rm_a)])
    assert payload.get("peak_equity") == pytest.approx(12000.0), (
        "the high-water mark must be persisted with the rest of the "
        f"trading-guard state, got {payload.get('peak_equity')!r}"
    )

    # --- restart: a brand-new RiskManager, the same Redis, a LOWER balance ---
    rm_b = _risk_manager(FakeRedis(redis_store), balance=11000.0)
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert _peak_of(rm_b) == pytest.approx(12000.0), (
        "the high-water mark was re-derived from the live balance instead of "
        f"being restored, got {_peak_of(rm_b)!r}"
    )
    assert _peak_set_at(rm_b) == pytest.approx(peak_at), (
        "the peak's own timestamp must travel with it, so the window is "
        "measured from when the peak was actually set"
    )


async def test_the_restored_peak_still_closes_the_gate(redis_store):
    """Restoring the number is not enough -- it must still be ENFORCED.

    A fix that restores the peak but forgets to re-evaluate the limit would
    leave the number right and the account trading.
    """
    rm_a = _risk_manager(FakeRedis(redis_store), balance=12000.0)
    await rm_a.initialize_balance()
    await rm_a.save_state()

    rm_b = _risk_manager(FakeRedis(redis_store), balance=10000.0)
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert rm_b._is_trading_allowed is False, (
        "the account is 16.67% below its high-water mark, so the restored "
        "peak must close the gate"
    )
    reason = rm_b.get_trading_disabled_reason()
    assert reason and "drawdown" in reason.lower(), (
        f"the gate must name the drawdown as the cause, got {reason!r}"
    )


# ---------------------------------------------------------------------------
# 2. The peak is NOT reset at the UTC rollover
# ---------------------------------------------------------------------------


async def test_the_peak_is_not_reset_at_the_utc_rollover(redis_store):
    """The daily rollover must not clear the high-water mark.

    The rollover resets the day's PnL and the loss streak, because those
    are DAY quantities. The peak is not a day quantity: it is the account's
    high-water mark, and clearing it every midnight would collapse the
    peak-to-trough limit back into the daily-loss limit.
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0)
    await rm.initialize_balance()
    peak_before = _peak_of(rm)
    assert peak_before == pytest.approx(10000.0)

    # Roll the UTC day over under the RiskManager's feet.
    rm._get_current_day_start_info = lambda: (
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
        "2026-10-21",
    )
    rm.stats.last_known_day_str = "2026-10-20"
    rm._check_and_reset_daily_stats()

    assert rm.stats.today_pnl == 0.0, "the day's PnL must be reset"
    assert _peak_of(rm) == pytest.approx(peak_before), (
        "the UTC rollover cleared the high-water mark -- the peak-to-trough "
        f"limit would then be the daily-loss limit again, got {_peak_of(rm)!r}"
    )


# ---------------------------------------------------------------------------
# 3. A genuine peak-to-trough breach closes the gate
# ---------------------------------------------------------------------------


async def test_a_genuine_peak_to_trough_breach_closes_the_gate(redis_store):
    """The breach must close the gate and NAME the peak and the trough."""
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm.initialize_balance()
    assert _peak_of(rm) == pytest.approx(10000.0)

    # The account falls 12% below its high-water mark.
    rm.stats.current_balance = 8800.0
    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a 12% fall from the high-water mark must close a 10% drawdown gate"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and "drawdown" in reason.lower(), (
        f"the gate must name the drawdown as the cause, got {reason!r}"
    )
    assert "12.00%" in reason, (
        f"the measured drawdown must be reported, got: {reason!r}"
    )
    assert "10.00%" in reason, (
        f"the threshold must be reported, got: {reason!r}"
    )
    assert "10000.00" in reason and "8800.00" in reason, (
        "the reason must name BOTH the peak and the trough, so the operator "
        f"can see what was measured against what, got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# 4. The case that is IMPOSSIBLE today: down on the peak, up on the day
# ---------------------------------------------------------------------------


async def test_a_drawdown_below_the_high_water_mark_closes_the_gate_even_when_today_made_money(
    redis_store,
):
    """THE case the old expression cannot express.

    The account is 12% below its high-water mark but made money TODAY, so
    ``today_pnl`` is POSITIVE. The old code computed
    ``abs(today_pnl / start_of_day_balance)`` -> 0% -> the gate stayed OPEN
    and the account kept trading its way down.
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm.initialize_balance()
    assert _peak_of(rm) == pytest.approx(10000.0)

    # The balance was read AFTER today's win, so it already contains it.
    rm.stats.current_balance = 8800.0
    rm.stats.today_pnl = 100.0
    rm._set_balance_pnl_baseline()
    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "the account is 12% below its high-water mark; a profitable day must "
        "not make the peak-to-trough limit read 0% and let it keep trading"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and "drawdown" in reason.lower(), (
        f"the gate must name the drawdown as the cause, got {reason!r}"
    )
    assert "12.00%" in reason, (
        f"the drawdown must be measured from the peak to the balance, got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# 5. The daily-loss limit is NOT weakened
# ---------------------------------------------------------------------------


async def test_the_daily_loss_limit_still_fires_at_exactly_five_percent(redis_store):
    """The daily-loss check must keep its own expression, threshold and order.

    The peak here is far above the balance (200k against a 95k balance), so
    the two limits are measuring genuinely different quantities. The daily
    loss is exactly 5.00% against a 5.00% limit, so the daily-loss limit
    must fire -- and it must still be the reason the operator is shown.
    """
    rm = _risk_manager(
        FakeRedis(redis_store), balance=100000.0, max_drawdown=10.0, daily_loss=5.0
    )
    await rm.initialize_balance()
    _set_todays_opening_balance(rm, 100000.0)

    rm.stats.current_balance = 95000.0
    rm.stats.today_pnl = -5000.0  # exactly 5.00% of the day's opening balance
    rm._set_balance_pnl_baseline()
    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a 5.00% daily loss against a 5.00% limit must close the gate"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and "Daily loss (5.00%)" in reason, (
        "the daily-loss limit must still fire, with its own reason string, "
        f"got {reason!r}"
    )


# ---------------------------------------------------------------------------
# 6. A rise-then-fall to just above the peak does NOT trip
# ---------------------------------------------------------------------------


async def test_a_rise_then_fall_to_just_above_the_peak_does_not_trip(redis_store):
    """The limit must not fire on a healthy account.

    The balance rises to 10500 (the high-water mark moves up with it) and
    then falls back to 10400 -- just above the peak. That is a 0.95%
    drawdown, far inside a 10% limit.
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm.initialize_balance()

    rm.stats.current_balance = 10500.0
    rm._observe_balance_for_peak(10500.0)
    assert _peak_of(rm) == pytest.approx(10500.0), (
        "the high-water mark must move UP with the balance, got "
        f"{_peak_of(rm)!r}"
    )

    rm.stats.current_balance = 10400.0
    rm._check_risk_limits()

    assert rm._is_trading_allowed is True, (
        "a fall to just above the high-water mark is a 0.95% drawdown and "
        "must not close a 10% gate"
    )


# ---------------------------------------------------------------------------
# 7. An unreadable peak FAILS CLOSED
# ---------------------------------------------------------------------------


async def test_an_unreadable_peak_fails_closed(redis_store):
    """A peak that cannot be read must close the gate, not read as 0%.

    The persisted payload carries a peak that is not a number. The reader
    cannot say what the high-water mark was, and "cannot be computed" is
    NOT "within limits" -- the same rule the unknown opening balance
    follows.
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    # The balance is known, but NO balance OBSERVATION has established a
    # high-water mark yet.
    rm.stats.current_balance = 10000.0

    redis = FakeRedis(redis_store)
    redis.store[_guard_key(rm)] = json.dumps(
        {
            "version": 3,
            "day_str": _today_utc(),
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": 10000.0,
            "start_of_day_day_str": _today_utc(),
            "peak_equity": "not-a-number",
            "peak_equity_set_at": "not-a-number",
        }
    )
    rm.redis_client = redis

    await rm.restore_state()

    assert rm._is_trading_allowed is False, (
        "a peak that cannot be read must fail CLOSED -- reading it as 'no "
        "peak' would mean 'no drawdown' and would open the gate"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and PEAK_UNKNOWN_REASON_FRAGMENT in reason, (
        "the gate must name the unknown high-water mark as the cause, got "
        f"{reason!r}"
    )


async def test_a_payload_without_a_peak_does_not_read_as_no_peak(redis_store):
    """A pre-v3 payload carries no peak at all.

    It must be read as "the peak is UNKNOWN", not as "the peak is zero" and
    therefore "there is no drawdown".
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    rm.stats.current_balance = 10000.0

    redis = FakeRedis(redis_store)
    redis.store[_guard_key(rm)] = json.dumps(
        {
            "version": 2,
            "day_str": _today_utc(),
            "consecutive_losses": 0,
            "today_pnl": 0.0,
            "start_of_day_balance": 10000.0,
            "start_of_day_day_str": _today_utc(),
        }
    )
    rm.redis_client = redis

    await rm.restore_state()

    assert rm._is_trading_allowed is False, (
        "a payload with no peak must not be read as 'no drawdown'"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and PEAK_UNKNOWN_REASON_FRAGMENT in reason, (
        "the gate must name the unknown high-water mark as the cause, got "
        f"{reason!r}"
    )


# ---------------------------------------------------------------------------
# 8. The peak is scoped per account and per api key
# ---------------------------------------------------------------------------


async def test_the_peak_is_scoped_per_user_and_api_key(redis_store):
    """One account's high-water mark must never become another's.

    The persisted state is scoped per user AND per api key, exactly like
    the portfolio key, so two controllers cannot overwrite each other's
    guard state. The peak travels in that payload, so it inherits the
    scoping -- and must be checked for it.
    """
    redis = FakeRedis(redis_store)

    rm_a = _risk_manager(redis, user_id=7, api_key_id=1, balance=12000.0)
    await rm_a.initialize_balance()
    await rm_a.save_state()

    # A different api key of the SAME user.
    rm_b = _risk_manager(redis, user_id=7, api_key_id=2, balance=9000.0)
    await rm_b.initialize_balance()
    await rm_b.save_state()

    # A different user, same api key id.
    rm_c = _risk_manager(redis, user_id=8, api_key_id=1, balance=11000.0)
    await rm_c.initialize_balance()
    await rm_c.restore_state()

    assert _peak_of(rm_c) == pytest.approx(11000.0), (
        "user 8 inherited user 7's high-water mark, got " f"{_peak_of(rm_c)!r}"
    )

    # And the first controller's own peak must still be its own.
    rm_a2 = _risk_manager(redis, user_id=7, api_key_id=1, balance=11000.0)
    await rm_a2.initialize_balance()
    await rm_a2.restore_state()

    assert _peak_of(rm_a2) == pytest.approx(12000.0), (
        "the second api key's write erased the first controller's "
        f"high-water mark, got {_peak_of(rm_a2)!r}"
    )


# ---------------------------------------------------------------------------
# The stale-balance trap: the trough must move when a trade closes
# ---------------------------------------------------------------------------


async def test_the_trough_moves_when_a_trade_closes(redis_store):
    """`update_trade_result` moves `today_pnl` and NOT the balance.

    The balance only moves when it is READ, so a peak-to-trough measured on
    the balance alone reads the PRE-TRADE balance at the exact moment the
    loss is realised -- and the drawdown limit under-trips: the account keeps
    trading until the next balance read, which may never come.

    The daily-loss limit is set out of the way here (99%) so that the drawdown
    is the ONLY thing that can close the gate.
    """
    rm = _risk_manager(
        FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0, daily_loss=99.0
    )
    await rm.initialize_balance()
    assert _peak_of(rm) == pytest.approx(10000.0)

    await rm.update_trade_result("BTCUSDT", -1200.0)

    assert rm.stats.current_balance == pytest.approx(10000.0), (
        "precondition: the balance did NOT move on the trade result"
    )
    assert rm._is_trading_allowed is False, (
        "the account is 12% below its high-water mark the moment the loss is "
        "realised, so the limit must close the gate there and then -- not at "
        "the next balance read"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and "12.00%" in reason, (
        "the drawdown must be measured against the balance as it stands, "
        f"got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# The window: the peak expires, and the account can trade again
# ---------------------------------------------------------------------------


def test_the_peak_window_is_seven_days_and_is_a_named_constant():
    """The window is a named, documented constant with the owner's value.

    The owner chose 7 days on 2026-11-01. This is asserted against the
    module attribute (not an import) so that a missing or renamed constant
    fails with a readable message instead of a collection error.
    """
    value = getattr(rm_module, "DRAWDOWN_PEAK_WINDOW_DAYS", None)
    assert value == OWNER_CHOSEN_PEAK_WINDOW_DAYS, (
        "the high-water-mark window must be a named constant set to "
        f"{OWNER_CHOSEN_PEAK_WINDOW_DAYS} days (the owner's decision of "
        f"2026-11-01), got {value!r}"
    )


async def test_a_peak_older_than_the_window_is_not_counted(redis_store):
    """A peak set 8 days ago is OUTSIDE the 7-day window.

    It must not be counted: the account fell 12% from a high-water mark
    that is no longer in force, so the limit must not fire on it and the
    account must be able to trade again. This is the reset path, and it is
    the reason the window is bounded.
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm.initialize_balance()

    rm.stats.current_balance = 8800.0
    rm._peak_equity = 10000.0
    rm._peak_equity_set_at = time.time() - (
        OWNER_CHOSEN_PEAK_WINDOW_DAYS + 1
    ) * 86400.0

    rm._check_risk_limits()

    assert rm._is_trading_allowed is True, (
        "a high-water mark older than the 7-day window must not close the "
        "gate -- the window is what makes the limit recoverable"
    )


async def test_a_peak_within_the_window_is_counted(redis_store):
    """The other half of the pair: a peak set yesterday IS counted.

    Without this the expiry test proves nothing -- a limit that never
    fires would pass it too.
    """
    rm = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm.initialize_balance()

    rm.stats.current_balance = 8800.0
    rm._peak_equity = 10000.0
    rm._peak_equity_set_at = time.time() - 86400.0  # one day old

    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a high-water mark one day old is inside the 7-day window and must "
        "be counted: the account is 12% below it"
    )
    reason = rm.get_trading_disabled_reason()
    assert reason and "drawdown" in reason.lower(), (
        f"the gate must name the drawdown as the cause, got {reason!r}"
    )


async def test_the_window_survives_a_restart(redis_store):
    """The peak's timestamp travels with it, so the window does too.

    A restart must not silently renew a stale peak's seven days.
    """
    rm_a = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm_a.initialize_balance()
    a_day_ago = time.time() - 86400.0
    rm_a._peak_equity = 10000.0
    rm_a._peak_equity_set_at = a_day_ago
    await rm_a.save_state()

    rm_b = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert _peak_set_at(rm_b) == pytest.approx(a_day_ago, abs=1.0), (
        "the peak's timestamp must be restored, not renewed by the restart"
    )


async def test_a_stale_peak_is_not_counted_after_a_restart(redis_store):
    """A peak that was already stale BEFORE the restart is still stale after it.

    The persisted peak is $20000, eight days old; the balance is $10000. A
    peak that were counted would read a 50% drawdown and close the gate. It
    must not be counted: the window expired, so the account's high-water mark
    for the new window is the balance as it stands now.
    """
    rm_a = _risk_manager(FakeRedis(redis_store), balance=20000.0, max_drawdown=10.0)
    await rm_a.initialize_balance()
    rm_a._peak_equity = 20000.0
    rm_a._peak_equity_set_at = time.time() - (
        OWNER_CHOSEN_PEAK_WINDOW_DAYS + 1
    ) * 86400.0
    await rm_a.save_state()

    rm_b = _risk_manager(FakeRedis(redis_store), balance=10000.0, max_drawdown=10.0)
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    rm_b.stats.current_balance = 10000.0
    rm_b._check_risk_limits()

    assert rm_b._is_trading_allowed is True, (
        "a high-water mark that was already outside the 7-day window before "
        "the restart must still be outside it after the restart"
    )
