"""Regression tests: the trading-guard state must survive a bot restart.

THE DEFECT
----------
``RiskManager.save_state()`` was literally::

    async def save_state(self):
        pass

so ``TradeStats.consecutive_losses`` and ``TradeStats.today_pnl`` lived in
RAM only. They were born again at zero on every process start, and
``initialize_balance()`` resets them to zero explicitly on the way in.

Measured in production on 2026-10-21: a routine deploy restart moved a
blocked account's ``consecutive_losses`` from 10 to 0 and the trading gate
from blocked to allowed. A bot that was correctly blocked for a losing
streak was silently unblocked by a routine deploy. That is the hole this
file pins shut.

These tests drive the REAL ``RiskManager`` -- not a stub. An earlier draft
of a sibling file faked ``rm``, which pinned the fake's semantics rather
than the product's. The end state asserted here is the real one:
``stats.consecutive_losses`` and ``_is_trading_allowed``.

THE GUARANTEE
-------------
A restart MUST NOT move a correctly blocked account from blocked to allowed.
"""

import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot_module import config
from bot_module.risk_manager import RiskManager
from bot_module.exchanges import ExchangeExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async stand-in for the bits of Redis the guard state uses.

    The store is a dict, and it is deliberately SHARED between the two
    RiskManagers below: handing the same store to a second RiskManager is
    exactly what "the bot restarted" means here. A fake that kept its state
    inside the RiskManager would not be able to express a restart at all.
    """

    def __init__(self, store=None):
        self.store = {} if store is None else store
        self.set_calls = []
        self.get_calls = []

    async def get(self, key):
        self.get_calls.append(key)
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


class FailingWriteRedis(FakeRedis):
    """Redis that fails on write."""

    async def set(self, key, value):
        raise ConnectionError("redis is down")


@pytest.fixture
def redis_store():
    """The shared Redis store -- the thing that outlives a process."""
    return {}


# The dedicated namespace for the bot's private guard state.
#
# Written as a LITERAL rather than read from ``config`` so that this file
# pins the wire contract independently of the implementation's plumbing -- and
# so that the persistence test fails on the unfixed code for the RIGHT
# reason ("nothing was written at all") instead of on a missing constant.
GUARD_STATE_NAMESPACE = "depthsight:state:risk"


def _risk_manager(redis_client, user_id=1, api_key_name="paper", api_key_id=None):
    """A real RiskManager with only the exchange handle faked.

    ``maxConsecutiveLosses`` is set explicitly to 5 so the test does not
    depend on ``config.DEFAULT_MAX_CONSECUTIVE_LOSSES`` changing underneath
    it. The executor returns a healthy balance so that the gate can only
    close for the reason under test -- the loss streak -- and not because the
    account is broke.
    """
    executor = AsyncMock(spec=ExchangeExecutor)
    executor.get_account_balance.return_value = {
        "USDT": {"free": "10000.00", "locked": "0.00"}
    }
    rm = RiskManager(
        executor=executor,
        paper_executor=None,
        user_id=user_id,
        db_session=None,
        user_settings={"risk_management": {"maxConsecutiveLosses": 5}},
        api_key_name=api_key_name,
    )
    # The RiskManager builds its own Redis client in __init__; swap in the
    # fake so the test can observe exactly what was written.
    rm.redis_client = redis_client
    # The controller hands the api key id over after the RiskManager is
    # built (the RiskManager is constructed first).
    rm.api_key_id = api_key_id
    return rm


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_utc():
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _guard_key(rm):
    return f"{GUARD_STATE_NAMESPACE}:{rm.user_id}:{rm.api_key_id}"


# ---------------------------------------------------------------------------
# The defect: a blocked account must stay blocked across a restart
# ---------------------------------------------------------------------------


async def test_save_state_persists_the_guard_state(redis_store):
    """The guard state must reach Redis AT ALL.

    This is the behavioural half of the defect: ``save_state()`` was a
    no-op, so nothing was ever written and nothing could ever be restored.
    Asserting on the ARTIFACT (rather than on the restore) keeps this test
    honest -- it fails on the unfixed code with a message that names the
    actual bug, instead of failing because a method is missing.
    """
    rm = _risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    rm.stats.consecutive_losses = 10
    rm.stats.today_pnl = -50.0

    await rm.save_state()

    key = _guard_key(rm)
    assert key in redis_store, (
        "save_state() wrote nothing to Redis, so the loss streak cannot "
        "possibly survive a restart -- this is the exact production defect"
    )
    payload = json.loads(redis_store[key])
    assert payload["consecutive_losses"] == 10
    assert payload["today_pnl"] == -50.0


async def test_the_loss_streak_survives_a_bot_restart(redis_store):
    """THE regression.

    RiskManager A carries a 10-loss streak and is correctly blocked. A bot
    restart builds RiskManager B. B must come up with the SAME streak and
    with the gate STILL CLOSED.
    """
    rm_a = _risk_manager(FakeRedis(redis_store))
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    rm_a.stats.today_pnl = -50.0
    await rm_a.save_state()

    # The state must have reached Redis, or nothing below can mean anything.
    assert _guard_key(rm_a) in redis_store, (
        "save_state() wrote nothing to Redis, so the loss streak cannot "
        "possibly survive a restart -- this is the exact production defect"
    )

    # --- restart: a brand-new RiskManager, the same Redis ---
    rm_b = _risk_manager(FakeRedis(redis_store))
    await rm_b.initialize_balance()
    restored = await rm_b.restore_state()

    assert restored is True, "the persisted guard state was not restored"
    assert rm_b.stats.consecutive_losses == 10, (
        "the loss streak did not survive the restart -- it was reset to zero, "
        "so a correctly blocked account silently starts trading again"
    )


async def test_the_gate_is_still_closed_after_a_restart(redis_store):
    """Restoring the streak must also leave new entries blocked.

    Asserting only the counter would let a fix that restores the number but
    forgets to re-evaluate the gate pass: the number would be right and the
    account would still trade.
    """
    rm_a = _risk_manager(FakeRedis(redis_store))
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()
    assert _guard_key(rm_a) in redis_store, (
        "save_state() wrote nothing to Redis, so the gate cannot be re-closed "
        "after a restart"
    )

    rm_b = _risk_manager(FakeRedis(redis_store))
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert rm_b._is_trading_allowed is False, (
        "the trading gate must still be closed after a restart -- the account "
        "was blocked by the loss streak and a deploy must not un-block it"
    )
    reason = rm_b.get_trading_disabled_reason()
    assert reason and "consec losses" in reason, (
        f"the gate must name the loss streak as the cause, got {reason!r}"
    )


async def test_the_days_pnl_survives_a_restart(redis_store):
    """The day's PnL is half of the guard state."""
    rm_a = _risk_manager(FakeRedis(redis_store))
    await rm_a.initialize_balance()
    rm_a.stats.today_pnl = -123.45
    await rm_a.save_state()

    rm_b = _risk_manager(FakeRedis(redis_store))
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert rm_b.stats.today_pnl == -123.45


async def test_the_start_of_day_balance_survives_a_restart(redis_store):
    """The daily-loss and drawdown limits are a RATIO against this number.

    Restoring the PnL without its baseline would measure it against the
    wrong denominator and could close (or open) the gate on a fiction.
    """
    rm_a = _risk_manager(FakeRedis(redis_store))
    await rm_a.initialize_balance()
    rm_a.stats.today_pnl = -50.0
    rm_a.stats.start_of_day_balance = 20000.0
    await rm_a.save_state()

    rm_b = _risk_manager(FakeRedis(redis_store))
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert rm_b.stats.start_of_day_balance == 20000.0


# ---------------------------------------------------------------------------
# Positive control -- do not invent a state that was never asked for
# ---------------------------------------------------------------------------


async def test_a_clean_restart_leaves_the_gate_open(redis_store):
    """A first boot must not block trading.

    Without this, a controller that stopped every user on every boot would
    pass the other tests while being unusable.
    """
    rm = _risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()

    restored = await rm.restore_state()

    assert restored is False, "nothing was persisted, so nothing was restored"
    assert rm.stats.consecutive_losses == 0
    assert rm._is_trading_allowed is True


async def test_the_state_is_scoped_per_user_and_api_key(redis_store):
    """Two controllers must not overwrite each other's streak."""
    rm_a = _risk_manager(FakeRedis(redis_store), user_id=7, api_key_id=1)
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    rm_b = _risk_manager(FakeRedis(redis_store), user_id=8, api_key_id=1)
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert rm_b.stats.consecutive_losses == 0, (
        "user 8 must not inherit user 7's loss streak"
    )
    assert rm_a.stats.consecutive_losses == 10


async def test_two_api_keys_with_the_same_name_do_not_share_a_streak(redis_store):
    """Api key NAMES are not unique per user -- the IDs are.

    ``api_keys`` has no unique constraint on ``(user_id, name)``, so one user
    can own two api keys called "default". If the guard state were scoped by
    name, those two controllers would share one Redis key: the second
    controller's write would erase the first controller's streak, and the
    account would be un-blocked on the next restart -- the exact defect this
    change exists to close.
    """
    rm_a = _risk_manager(
        FakeRedis(redis_store), user_id=7, api_key_name="default", api_key_id=1
    )
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    rm_b = _risk_manager(
        FakeRedis(redis_store), user_id=7, api_key_name="default", api_key_id=2
    )
    await rm_b.initialize_balance()
    rm_b.stats.consecutive_losses = 0
    await rm_b.save_state()

    assert _guard_key(rm_a) != _guard_key(rm_b), (
        "two api keys of the same user must not share one guard key"
    )

    # And the first controller's streak must still be there for its own key.
    rm_a2 = _risk_manager(
        FakeRedis(redis_store), user_id=7, api_key_name="default", api_key_id=1
    )
    await rm_a2.initialize_balance()
    await rm_a2.restore_state()

    assert rm_a2.stats.consecutive_losses == 10, (
        "the second controller's write erased the first controller's streak -- "
        "they share a guard key"
    )


# ---------------------------------------------------------------------------
# The UTC day boundary
# ---------------------------------------------------------------------------


async def test_a_previous_day_state_is_not_resurrected(redis_store):
    """The daily rollover clears the streak and the day's PnL at midnight.

    A state persisted yesterday must not come back today: it would
    re-block an account whose streak was legitimately cleared, and would
    carry a stale PnL into a new trading day.
    """
    redis = FakeRedis(redis_store)
    rm = _risk_manager(redis)
    await rm.initialize_balance()
    await redis.set(
        _guard_key(rm),
        json.dumps(
            {
                "version": 1,
                "day_str": _yesterday_utc(),
                "consecutive_losses": 10,
                "today_pnl": -500.0,
                "start_of_day_balance": 10000.0,
            }
        ),
    )
    # The stale payload MUST be in the store, or this test would pass for
    # the wrong reason (an absent payload is discarded too).
    assert _guard_key(rm) in redis_store

    restored = await rm.restore_state()

    assert restored is False, "a previous day's state must be discarded"
    assert rm.stats.consecutive_losses == 0
    assert rm.stats.today_pnl == 0.0
    assert rm._is_trading_allowed is True


async def test_the_persisted_state_carries_its_utc_day(redis_store):
    """The day stamp is what makes the rule above possible."""
    rm = _risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    rm.stats.consecutive_losses = 3
    await rm.save_state()

    payload = json.loads(redis_store[_guard_key(rm)])
    assert payload["day_str"] == _today_utc()


# ---------------------------------------------------------------------------
# Fail-closed semantics
# ---------------------------------------------------------------------------


async def test_an_unreadable_state_fails_closed(redis_store):
    """If we cannot prove the streak is clear, keep trading blocked.

    A verification failure must never turn into permission to trade. This is
    the one place the code deliberately trades availability for safety, so it
    is pinned by a test rather than left to a comment.
    """
    rm = _risk_manager(ExplodingRedis(redis_store))
    await rm.initialize_balance()
    rm.stats.consecutive_losses = 0

    await rm.restore_state()

    assert rm._is_trading_allowed is False, (
        "an unreadable guard state must fail CLOSED, not open"
    )
    assert rm._guard_state_unverified is True


async def test_a_read_failure_does_not_open_the_gate_on_a_later_refresh(redis_store):
    """The fail-closed state must be STICKY.

    ``_check_risk_limits()`` runs on every balance refresh and would
    otherwise re-open the gate, because with an unknown streak no limit
    looks breached.
    """
    rm = _risk_manager(ExplodingRedis(redis_store))
    await rm.initialize_balance()
    await rm.restore_state()

    rm._check_risk_limits()

    assert rm._is_trading_allowed is False, (
        "a balance refresh must not re-open a gate whose guard state could "
        "not be verified"
    )


async def test_the_fail_closed_latch_is_released_once_the_state_can_be_read(
    redis_store,
):
    """A Redis blip must not cost the account its whole trading day.

    This is the difference between this latch and the operator's emergency
    stop: the emergency stop needs a human, this one only needs Redis to
    come back. The repo already paid for the difference once -- a start-up
    fail-closed emergency stop latched on 2026-10-19 and cost a full
    trading day of "general block" rejections.
    """
    redis = FakeRedis(redis_store)
    rm_a = _risk_manager(redis)
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    rm_b = _risk_manager(ExplodingRedis(redis_store))
    await rm_b.initialize_balance()
    await rm_b.restore_state()
    assert rm_b._is_trading_allowed is False

    # Redis comes back.
    rm_b.redis_client = redis
    await rm_b.save_state()

    assert rm_b._guard_state_unverified is False
    assert rm_b.stats.consecutive_losses == 10, (
        "the persisted streak must be recovered once the state is readable "
        "again"
    )
    assert rm_b._is_trading_allowed is False, (
        "the recovered streak still breaches the limit, so the gate stays "
        "closed"
    )


async def test_an_unverifiable_state_is_never_overwritten_with_a_zero(redis_store):
    """The untrusted in-memory zero must not destroy the persisted truth.

    While the state is unverifiable the in-memory counters are a guess. If
    they were written, a transient Redis blip would turn into PERMANENT
    data loss -- the exact defect this change fixes, one layer down.
    """
    redis = FakeRedis(redis_store)
    rm_a = _risk_manager(redis)
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    rm_b = _risk_manager(ExplodingRedis(redis_store))
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    # Redis is still down for the write, so nothing is written.
    await rm_b.save_state()
    assert json.loads(redis_store[_guard_key(rm_a)])["consecutive_losses"] == 10


async def test_a_lost_state_is_never_overwritten_with_a_zero(redis_store):
    """A LOST guard state must not be laundered into an empty streak.

    The permanent form of the fail-closed latch: the persisted guard state
    has VANISHED for an account that has traded before, so today's PnL and
    the loss streak are UNKNOWN, not zero. The gate is closed and the
    `_guard_state_lost` latch is set.

    The in-memory counters are still the zeros this process was born with.
    If they are persisted, the next start-up finds a payload, takes the
    normal restore path, restores streak=0 / pnl=0, and OPENS the gate --
    the product's own periodic save would launder an unknown state into
    "the streak really is empty", which is precisely the fail-open the
    lost-state latch exists to prevent.

    Found by the DSH reviewer during the free-tier review fan-out and
    verified against the committed tree at 035b0d2. This mirrors the
    analogous `_guard_state_unverified` guard directly above.
    """
    redis = FakeRedis(redis_store)
    rm_a = _risk_manager(redis)
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    # The guard key vanishes but the "has traded before" marker survives:
    # this is a LOST state, not a first boot.
    redis_store.pop(_guard_key(rm_a))

    rm_b = _risk_manager(redis)
    # The controller restores BEFORE it initializes the balance.
    assert await rm_b.restore_state() is False
    assert rm_b._guard_state_lost is True
    assert rm_b._is_trading_allowed is False

    # `initialize_balance()` zeroes the in-memory counters -- the state is
    # unknown, not zero, so those zeros must never reach Redis.
    await rm_b.initialize_balance()
    await rm_b.save_state()
    assert _guard_key(rm_a) not in redis_store, (
        "the lost guard state was overwritten with the in-memory zeros; the "
        "next start-up would read the loss as an empty streak and open the gate"
    )

    # And the next start-up must still see a LOST state, not a clean one.
    rm_c = _risk_manager(redis)
    assert await rm_c.restore_state() is False
    assert rm_c._guard_state_lost is True
    assert rm_c._is_trading_allowed is False


async def test_clearing_the_emergency_stop_does_not_resume_a_blocked_account(
    redis_store,
):
    """An operator's "clear the kill switch" must not resume a blocked account.

    Found by the DSH reviewer during the free-tier review fan-out, and
    verified against the committed tree: ``clear_emergency_stop()`` sets
    ``_is_trading_allowed = True`` unconditionally, with no re-evaluation of
    the risk limits. Before the guard state was persisted this was harmless,
    because the streak was always zero in RAM after a restart. With the
    streak RESTORED it is not: the operator clears a kill switch and an
    account that is still over its loss limit resumes trading.

    The latch is still released -- that is what the operator asked for. What
    must not happen is the RESTORED limit being overridden.
    """
    redis = FakeRedis(redis_store)
    rm_a = _risk_manager(redis)
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    rm_b = _risk_manager(redis)
    await rm_b.initialize_balance()
    await rm_b.restore_state()
    assert rm_b._is_trading_allowed is False

    rm_b.trigger_emergency_stop("operator pulled the plug")
    rm_b.clear_emergency_stop()

    assert rm_b.is_emergency_stop_active() is False, (
        "the operator's clear must still release the latch"
    )
    assert rm_b._is_trading_allowed is False, (
        "clearing the emergency stop must not open a gate that a RESTORED "
        "loss streak is still holding closed -- the restored streak is the "
        "account's real history, not an operator decision"
    )
    reason = rm_b.get_trading_disabled_reason()
    assert reason and "consec losses" in reason, (
        f"the gate must still name the loss streak, got {reason!r}"
    )


async def test_clearing_the_emergency_stop_does_not_open_an_unverified_gate(
    redis_store,
):
    """The fail-closed latch must also survive the operator's clear."""
    rm = _risk_manager(ExplodingRedis(redis_store))
    await rm.initialize_balance()
    await rm.restore_state()
    assert rm._is_trading_allowed is False

    rm.trigger_emergency_stop("operator pulled the plug")
    rm.clear_emergency_stop()

    assert rm.is_emergency_stop_active() is False
    assert rm._is_trading_allowed is False, (
        "clearing the emergency stop must not open a gate whose guard state "
        "could not be verified"
    )


async def test_clearing_the_emergency_stop_still_resumes_a_healthy_account(
    redis_store,
):
    """Positive control: the operator's clear must still work.

    Without this, a fix that simply never re-opened the gate would pass the
    tests above and leave the operator unable to resume trading.
    """
    rm = _risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    rm.trigger_emergency_stop("operator pulled the plug")
    assert rm._is_trading_allowed is False

    rm.clear_emergency_stop()

    assert rm.is_emergency_stop_active() is False
    assert rm._is_trading_allowed is True


# ---------------------------------------------------------------------------
# The invariant must not depend on the order of the start-up calls
# ---------------------------------------------------------------------------


async def test_a_restored_streak_survives_a_later_initialize_balance(redis_store):
    """``initialize_balance()`` must not wipe a restored guard state.

    It re-baselines the trading day on EVERY successful call -- not only on a
    real day change -- and the controller calls it on the same start as the
    restore. If it could wipe the restored streak, the restore would be
    undone one layer down and the guarantee would hold only for a particular
    call order.
    """
    redis = FakeRedis(redis_store)
    rm_a = _risk_manager(redis)
    await rm_a.initialize_balance()
    rm_a.stats.consecutive_losses = 10
    await rm_a.save_state()

    rm_b = _risk_manager(redis)
    await rm_b.initialize_balance()
    await rm_b.restore_state()
    assert rm_b.stats.consecutive_losses == 10

    # The controller calls initialize_balance() again on the same start.
    await rm_b.initialize_balance()

    assert rm_b.stats.consecutive_losses == 10, (
        "initialize_balance() re-baselined the trading day and wiped the "
        "restored loss streak -- the restore would be undone on the same start"
    )
    assert rm_b._is_trading_allowed is False


async def test_the_fail_closed_state_is_not_reopened_by_initialize_balance(
    redis_store,
):
    """The fail-closed gate must survive the start-up sequence too.

    ``initialize_balance()`` re-opens the gate on its day-reset branch. While
    the guard state is UNVERIFIABLE that must not happen -- the whole point of
    failing closed is that the gate stays closed until the state can be read.
    """
    rm = _risk_manager(ExplodingRedis(redis_store))
    await rm.initialize_balance()
    await rm.restore_state()
    assert rm._is_trading_allowed is False

    await rm.initialize_balance()

    assert rm._is_trading_allowed is False, (
        "initialize_balance() re-opened a gate whose guard state could not be "
        "verified -- a verification failure must never become permission to "
        "trade"
    )


# ---------------------------------------------------------------------------
# Redis unavailability at runtime
# ---------------------------------------------------------------------------


async def test_save_state_without_a_redis_client_does_not_raise():
    """``self.redis_client`` is None when the Redis init failed.

    The save path must not crash the caller: the controller awaits it on
    its periodic tick and from the balance-update handler.
    """
    rm = _risk_manager(None)
    rm.redis_client = None
    await rm.initialize_balance()
    rm.stats.consecutive_losses = 10

    await rm.save_state()  # must not raise


async def test_restore_state_without_a_redis_client_does_not_raise():
    """The restore path must not crash the start-up sequence either."""
    rm = _risk_manager(None)
    rm.redis_client = None
    await rm.initialize_balance()

    await rm.restore_state()  # must not raise


async def test_a_failed_write_does_not_raise(redis_store):
    """A Redis outage must not take the trading loop down.

    The in-memory guard must keep working; only the durability is lost, and
    that is logged CRITICAL rather than raised.
    """
    rm = _risk_manager(FailingWriteRedis(redis_store))
    await rm.initialize_balance()
    rm.stats.consecutive_losses = 10

    await rm.save_state()  # must not raise

    assert rm.stats.consecutive_losses == 10


# ---------------------------------------------------------------------------
# What is deliberately NOT persisted
# ---------------------------------------------------------------------------


async def test_the_emergency_stop_is_not_duplicated_into_the_guard_key(
    redis_store,
):
    """The emergency stop already has its own durable, authoritative key.

    ``emergency_stop:{user_id}`` is written by the API and restored by the
    controller, with a deliberate distinction between an operator's kill
    switch and an infrastructure failure. A second copy here would be a
    second source of truth that could resurrect a stop the operator had
    already cleared.
    """
    redis = FakeRedis(redis_store)
    rm = _risk_manager(redis)
    await rm.initialize_balance()
    rm.trigger_emergency_stop("operator hit the kill switch")

    await rm.save_state()

    payload = json.loads(redis_store[_guard_key(rm)])
    assert "emergency" not in json.dumps(payload).lower(), (
        "the emergency-stop latch must not be mirrored into the guard key"
    )
    assert not any(
        "emergency" in key.lower() for key in redis_store
    ), "the guard key must not shadow the emergency-stop key"


async def test_a_cleared_emergency_stop_is_not_resurrected_by_the_guard_key(
    redis_store,
):
    """The round trip: stop -> resume -> restart must not re-stop the account."""
    redis = FakeRedis(redis_store)
    rm_a = _risk_manager(redis)
    await rm_a.initialize_balance()
    rm_a.trigger_emergency_stop("incident")
    rm_a.clear_emergency_stop()
    await rm_a.save_state()

    rm_b = _risk_manager(redis)
    await rm_b.initialize_balance()
    await rm_b.restore_state()

    assert rm_b.is_emergency_stop_active() is False
    assert rm_b._is_trading_allowed is True


# ---------------------------------------------------------------------------
# The contracts this change must not break
# ---------------------------------------------------------------------------


def test_the_guard_state_uses_a_dedicated_key():
    """The portfolio key is the published view; its shape must not change.

    PR #25 landed and the API reads ``REDIS_STATE_KEY_PORTFOLIO``. The guard
    state therefore lives under its OWN namespace.
    """
    assert config.REDIS_STATE_KEY_PORTFOLIO == "depthsight:state:portfolio"
    assert config.REDIS_STATE_KEY_RISK == "depthsight:state:risk"
    assert config.REDIS_STATE_KEY_RISK != config.REDIS_STATE_KEY_PORTFOLIO


async def test_save_state_keeps_its_signature():
    """Ten existing test files do ``rm.save_state = AsyncMock()``.

    They must keep working, so the method has to stay an awaitable
    ``save_state(self)`` with no required arguments.
    """
    signature = inspect.signature(RiskManager.save_state)
    assert list(signature.parameters) == ["self"], (
        "save_state must keep the signature save_state(self)"
    )
    assert inspect.iscoroutinefunction(RiskManager.save_state), (
        "save_state must stay a coroutine function"
    )

    rm = _risk_manager(FakeRedis({}))
    rm.save_state = AsyncMock()
    await rm.save_state()
    rm.save_state.assert_awaited_once()


async def test_the_portfolio_published_payload_is_untouched(redis_store):
    """The bot must not start writing the guard state into the portfolio key.

    The portfolio payload is the API's published view and PR #25 fixed the
    API's reading of it. Its shape must not change.
    """
    rm = _risk_manager(FakeRedis(redis_store))
    await rm.initialize_balance()
    rm.stats.consecutive_losses = 10

    await rm.save_state()

    portfolio_prefix = f"{config.REDIS_STATE_KEY_PORTFOLIO}:"
    assert not any(
        key.startswith(portfolio_prefix) for key in redis_store
    ), "the guard state must not be written into the portfolio key"
