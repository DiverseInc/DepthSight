"""The daily-loss gate's NUMERATOR must be the wallet's day PnL, and its
DENOMINATOR must never be invented by a read.

THE DEFECT (measured in production on 2026-10-24, one scope)
-------------------------------------------------------------
The daily-loss gate at ``risk_manager.py`` is a RATIO::

    daily_loss_pct = abs(today_pnl / start_of_day_balance)

Two independent defects made it read LOW, i.e. under-trip:

**Cause A -- the NUMERATOR was short by every entry commission.**
Paper trading books a fill's net PnL into ``position.pnl`` only on the EXIT
leg. The entry leg was never booked, so the risk manager's ``today_pnl`` was
missing the entry commission the wallet had actually been charged::

    RM loss = DB loss - entry commissions

The DB computes today's PnL as ``sum(pnl - commission)`` over the Trade rows
(``api/routes/portfolio.py``), which is why the API and the wallet agreed
while the risk manager did not. Measured: a true day loss of 8.2767% was
published as 6.17% against a 5% threshold -- the gap was $15.93 of entry
commissions. The true loss SHOULD have tripped the gate; it did not.

**Cause B/C -- the DENOMINATOR could be a $10,000 balance that a READ
invented.** ``GET /api/v1/portfolio?mode=paper``,
``GET /api/v1/account/paper`` and the paper executor's own balance read all
called ``init_or_reset_paper_wallet``, which creates a $10,000 wallet for an
account that has none. The risk manager then read that $10,000 as the
account's balance and stamped it as the day's opening balance: 5% of $10,000
is $500 of tolerated loss against 5% of a real ~$756.28 base = $37.81 -- a
13.2x disarm of the gate.

THE ORACLE IS THE MONEY
-----------------------
The paper wallet is credited ``realized_pnl - commission`` on every fill, and
the API reports ``sum(pnl - commission)`` over the Trade rows. So the number
the risk manager is given must be EXACTLY the number the wallet moved. These
tests drive the REAL ``PaperTradingExecutor``, the REAL controller
finalization and the REAL ``RiskManager``; the PnL arithmetic is not
re-implemented here.

Red-then-green: run this file against ``origin/main`` and the identity test
fails by exactly the entry commission.
"""

import asyncio
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from bot_module.controller import (
    ActivePositionMap,
    LivePosition,
    SignalDirection,
    TradingController,
)
from bot_module.paper_executor import PaperTradingExecutor
from bot_module.risk_manager import RiskManager
from bot_module.runtime_dependencies import crud

USER_ID = 7
SYMBOL = "BTCUSDT"
MARKET_TYPE = "futures_usdtm"

ENTRY_PRICE = 100.0
QTY = 1.0

# The paper book's commission (bot_module/config.py: BACKTEST_COMMISSION_PCT)
# and slippage, so the expected numbers are derived, not hard-coded.
COMMISSION_PCT = 0.0006
SLIPPAGE_PCT = 0.0005


def _utc_day_str(offset_days: int = 0) -> str:
    """The `YYYY-MM-DD` UTC day key the risk manager rolls over on."""
    day = datetime.now(timezone.utc).date() + timedelta(days=offset_days)
    return day.strftime("%Y-%m-%d")


# --- Test doubles: inputs only, never behaviour under test --------------------


class _FakeDataConsumer:
    """The only price source the paper executor reads."""

    def __init__(self, price: float):
        self.price = price

    async def get_latest_price(self, symbol):
        return self.price

    async def get_latest_depth(self, symbol):
        return None


class _FakeDB:
    """The paper executor's whole DB surface."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, obj, *args, **kwargs):
        return None


class _FakeTradeLogger:
    """Stand-in for the controller's trade_logger (it only has to not raise)."""

    def log_order_placed(self, *args, **kwargs):
        pass

    def log_event(self, *args, **kwargs):
        pass


class _FakeRedis:
    async def get(self, key):
        return None

    async def set(self, key, value):
        return True

    async def zadd(self, *args, **kwargs):
        return 1

    async def zremrangebyscore(self, *args, **kwargs):
        return 0


class _FakeLiveExecutor:
    """Stand-in for a real exchange connection."""

    market_type = MARKET_TYPE

    def __init__(self):
        self.cancelled = []

    async def cancel_order(self, **kwargs):
        self.cancelled.append(kwargs)
        return {"error": False}

    async def cancel_all_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return {"error": False}

    async def get_account_balance(self):
        return {"USDT": {"free": "1000.0", "locked": "0.0"}}


# --- The MONEY: the oracle ----------------------------------------------------


@pytest.fixture
def money(monkeypatch):
    """Records what the paper WALLET is credited/debited, and the DB rows.

    The wallet delta IS the net PnL of a fill: the executor credits
    `realized_pnl - actual_commission_paid`. It is the same number
    `api/routes/portfolio.py` shows the user as `sum(pnl - commission)`, so
    it is the number the risk manager has to agree with.
    """
    record = {"wallet": [], "trades": [], "wallet_balance": 1000.0}

    async def _fake_update_paper_wallet_balance(
        db, user_id, asset, amount, *args, **kwargs
    ):
        record["wallet"].append({"asset": asset, "amount": float(amount)})
        return None

    async def _fake_create_trade(db, *args, **kwargs):
        record["trades"].append(kwargs.get("trade_data") or {})
        return types.SimpleNamespace(id=4242)

    async def _fake_get_paper_wallet(db, user_id=None, *args, **kwargs):
        return [
            types.SimpleNamespace(asset="USDT", balance=float(record["wallet_balance"]))
        ]

    monkeypatch.setattr(
        crud, "update_paper_wallet_balance", _fake_update_paper_wallet_balance
    )
    monkeypatch.setattr(crud, "create_trade", _fake_create_trade)
    monkeypatch.setattr(crud, "get_paper_wallet", _fake_get_paper_wallet)

    # `place_order` imports the analytics task lazily. A celery app is noise
    # in a unit test, not behaviour under test.
    fake_tasks = types.ModuleType("tasks")
    fake_tasks.process_live_trade_analytics_task = MagicMock()
    monkeypatch.setitem(sys.modules, "tasks", fake_tasks)

    return record


def _wallet_total(money):
    """Everything the wallet moved, in order. The oracle."""
    return sum(w["amount"] for w in money["wallet"])


def _db_today_pnl(money):
    """What `api/routes/portfolio.py` reports: sum(pnl - commission)."""
    return sum(
        float(t.get("pnl") or 0.0) - float(t.get("commission") or 0.0)
        for t in money["trades"]
    )


# --- The world ----------------------------------------------------------------


def _build_controller(price: float):
    """A real RiskManager and a real PaperTradingExecutor, and a controller
    carrying only the attribute surface the code under test touches.

    Built with `__new__` because `TradingController.__init__` opens exchange
    connections; every method used below is the real, unbound production one.
    """
    data_consumer = _FakeDataConsumer(price)
    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=data_consumer,
        redis_client=None,
    )
    live_executor = _FakeLiveExecutor()

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = None
    controller.api_key_name = "paper"
    controller.executors = {"live": live_executor, "paper": paper_executor}
    controller.market_executors = {}
    controller.consumer = data_consumer
    controller.redis_client = _FakeRedis()
    controller.redis_key_runtime_state = f"runtime_state:{USER_ID}"
    controller.db_session = None
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller._symbol_locks = {}
    controller._market_info_cache = {}
    controller._market_info_lock = asyncio.Lock()
    controller._last_position_close_time_per_symbol = {}
    controller.telegram_notifier = None
    controller.user_telegram_chat_id = None
    controller.trade_logger = _FakeTradeLogger()
    controller.realtime_ml_logger = None
    controller._closing_managed_symbols = set()
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = {}

    controller.rm = RiskManager(
        executor=live_executor,
        paper_executor=paper_executor,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    paper_executor.controller = controller

    return controller, paper_executor, controller.rm


def _new_live_position(*, status="PENDING_ENTRY"):
    """The controller's mirror of a paper position, as the controller builds it."""
    return LivePosition(
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        entry_price=ENTRY_PRICE,
        initial_quantity=QTY,
        remaining_quantity=QTY,
        entry_time=time.time() - 3600,
        strategy="TestStrategy",
        status=status,
        mode="paper",
        market_type=MARKET_TYPE,
        user_id=None,
        config_id=6999,
        entry_client_order_id="x-entry-test-00000001",
        entry_order_id="2200861597",
        entry_order_status="FILLED",
    )


async def _run_real_entry(controller, paper_executor, *, qty=QTY):
    """A REAL paper market entry fill, through the real executor.

    The controller registers the position BEFORE it places the order
    (controller.py:9555-9556), so the mirror exists while the fill happens.
    """
    position = _new_live_position()
    controller._active_position_set(position)

    result = await paper_executor.place_order(
        symbol=SYMBOL,
        side="BUY",
        order_type="MARKET",
        quantity=qty,
        entry_client_order_id="x-entry-test-00000001",
        strategy_config_id=6999,
    )
    assert not result.get("error"), f"the entry fill failed: {result}"

    # The controller's own `_handle_entry_fill` flips PENDING_ENTRY -> OPEN
    # (controller.py:10092-10094) and records the entry commission
    # (controller.py:10129). That transition is the controller's job, not the
    # behaviour under test, so it is applied here directly.
    position.status = "OPEN"
    position.entry_commission = sum(
        float(f.get("commission") or 0.0) for f in result.get("fills", [])
    )
    return position, result


def _rest_exit_order(paper_executor, *, qty, price, order_type):
    """Rests the exit order the way the controller's paper book does."""
    client_order_id = f"x-{order_type.lower()}-test-00000001"
    paper_executor._open_orders[client_order_id] = {
        "symbol": SYMBOL,
        "orderId": 2200861599,
        "clientOrderId": client_order_id,
        "price": None if order_type == "STOP_MARKET" else str(price),
        "stopPrice": str(price) if order_type == "STOP_MARKET" else None,
        "origQty": str(qty),
        "executedQty": "0.0",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": order_type,
        "side": "SELL",
        "time": int(time.time() * 1000),
        "strategy_config_id": None,
        "signal_details": None,
    }
    return client_order_id


def _move_market_to(paper_executor, price: float) -> None:
    """Moves the fake market price.

    A SELL stop -- a stop-loss on a long -- is hit when the price FALLS to
    it: `check_open_orders` fires it only when `current_price <=
    order_price`. Resting a stop below a market that never moves leaves the
    stop untouched, so the exit NEVER happens and any assertion about the
    exit leg is vacuous. This is what makes the market move through the stop,
    which is what happens in production.
    """
    paper_executor.data_consumer.price = float(price)


async def _finalize(controller, reason: str):
    """Runs the REAL controller finalization of a paper close.

    A finalization-only close (`exit_price=0.0`): the money already moved in
    the fill, so the finalization preserves the PnL the fill booked instead
    of recomputing it.
    """
    await controller._handle_final_exit(
        SYMBOL,
        reason,
        exit_price=0.0,
        commission=0.0,
        commission_asset=None,
        order_id=None,
        client_order_id=None,
        realized_pnl_from_exchange=0.0,
        exchange_pnl_available=False,
        market_type=MARKET_TYPE,
    )


def _pin_day(rm):
    """Pin the day so the UTC rollover cannot fire and be mistaken for the
    effect under test."""
    rm.stats.last_known_day_str = _utc_day_str()
    rm.stats.current_balance = 1000.0
    rm.stats.start_of_day_balance = 1000.0


# --- 1. THE ROUND TRIP: the gate's numerator is the wallet's day PnL ---------


@pytest.mark.asyncio
async def test_the_risk_manager_today_pnl_is_the_wallet_and_the_db_after_an_entry_and_an_exit(
    money,
):
    """After an entry fill and an exit fill, the risk manager must be told the
    number the WALLET moved -- which is also the number the API reports.

    This is the round trip that was broken. The entry leg was charged a
    commission and never booked to the risk manager, so the gate's numerator
    was short by exactly that commission and the daily loss read LOW.
    """
    controller, paper_executor, rm = _build_controller(ENTRY_PRICE)
    _pin_day(rm)

    # A real entry fill: the wallet is debited the entry commission.
    position, entry_result = await _run_real_entry(controller, paper_executor)

    entry_commission = sum(
        float(f.get("commission") or 0.0) for f in entry_result.get("fills", [])
    )
    assert entry_commission > 0, (
        "the entry fill must have been charged a commission, or this test "
        "would not be testing anything"
    )

    # A real SL/TP exit fill: the wallet is credited the net of the close.
    # The market has to FALL to the stop for the stop to fire.
    _rest_exit_order(
        paper_executor, qty=QTY, price=ENTRY_PRICE * 0.99, order_type="STOP_MARKET"
    )
    _move_market_to(paper_executor, ENTRY_PRICE * 0.98)
    await paper_executor.check_open_orders()

    # The exit must ACTUALLY have happened. A test that asserts something
    # about an exit that never fired asserts nothing.
    assert len(money["wallet"]) == 2, (
        "the SL/TP exit never fired: a SELL stop is only triggered when the "
        "price falls to it, so the market has to move. Without this the "
        "assertions below would be about the entry leg alone."
    )
    assert len(money["trades"]) == 2, (
        f"expected an entry row and an exit row, got {len(money['trades'])}"
    )
    exit_row = money["trades"][1]
    assert float(exit_row.get("commission") or 0.0) > 0, (
        "the exit fill was not charged a commission, so it did not happen"
    )
    assert float(exit_row.get("pnl") or 0.0) < 0, (
        "the exit was supposed to be a LOSS (the price fell through the stop)"
    )

    # The real finalization hands `position.pnl` to the real risk manager.
    await _finalize(controller, "STOP_LOSS")

    wallet_total = _wallet_total(money)
    db_total = _db_today_pnl(money)

    assert db_total == pytest.approx(wallet_total), (
        "the wallet and the Trade rows must agree -- the API reports "
        f"sum(pnl - commission) = {db_total:.8f} while the wallet moved "
        f"{wallet_total:.8f}"
    )

    assert rm.stats.today_pnl == pytest.approx(wallet_total), (
        f"the risk manager was told {rm.stats.today_pnl:.8f} but the wallet "
        f"moved {wallet_total:.8f}. The gap is "
        f"{wallet_total - rm.stats.today_pnl:.8f}, which is the entry "
        f"commission ({entry_commission:.8f}) that was charged to the wallet "
        "and never booked. A daily-loss gate with a short numerator reads LOW "
        "and under-trips."
    )

    assert rm.stats.today_pnl == pytest.approx(db_total), (
        "the risk manager must be told the same number the API reports to the "
        "user"
    )


@pytest.mark.asyncio
async def test_the_entry_commission_is_in_the_risk_managers_day_pnl(money):
    """The entry commission must be visible to the gate, on its own.

    A tighter statement of the same defect: after the entry fill alone, the
    risk manager must already know about the commission the wallet paid.
    """
    controller, paper_executor, rm = _build_controller(ENTRY_PRICE)
    _pin_day(rm)

    position, entry_result = await _run_real_entry(controller, paper_executor)
    entry_commission = sum(
        float(f.get("commission") or 0.0) for f in entry_result.get("fills", [])
    )

    assert float(position.pnl or 0.0) == pytest.approx(-entry_commission), (
        f"the entry leg booked {position.pnl} but the wallet was charged "
        f"{entry_commission} of commission. An entry that is not booked leaves "
        "the gate's numerator short by every entry commission ever paid."
    )


# --- 2. NO DOUBLE COUNT: every leg is booked exactly once --------------------


@pytest.mark.asyncio
async def test_the_sl_tp_exit_leg_is_booked_exactly_once(money):
    """The SL/TP exit goes through `check_open_orders` -> `place_order` -> the
    notifier. Its leg must be booked ONCE, not twice and not zero times.

    Twice would make the gate over-trip; zero times would make it blind.
    """
    controller, paper_executor, rm = _build_controller(ENTRY_PRICE)
    _pin_day(rm)
    position, _ = await _run_real_entry(controller, paper_executor)

    entry_commission = -float(position.pnl or 0.0)

    _rest_exit_order(
        paper_executor, qty=QTY, price=ENTRY_PRICE * 0.99, order_type="STOP_MARKET"
    )
    # A SELL stop fires only when the price has fallen to it. Without this
    # move the stop never triggers, the exit never happens, and this test
    # would assert nothing about the exit leg at all.
    _move_market_to(paper_executor, ENTRY_PRICE * 0.98)
    await paper_executor.check_open_orders()

    # The exit MUST have happened. A green assertion about an exit that never
    # fired proves nothing.
    assert len(money["wallet"]) == 2, (
        "the SL/TP exit never fired: a SELL stop is only triggered when the "
        "price falls to it, so the market has to move"
    )
    assert len(money["trades"]) == 2, (
        f"expected an entry row and an exit row, got {len(money['trades'])}"
    )
    exit_row = money["trades"][1]
    assert float(exit_row.get("commission") or 0.0) > 0, (
        "the exit fill was not charged a commission, so it did not happen"
    )

    exit_net = _wallet_total(money) - (-entry_commission)

    assert exit_net < 0, (
        f"the exit leg was {exit_net}, which is not a losing close -- the "
        "market did not fall through the stop"
    )

    assert float(position.pnl or 0.0) == pytest.approx(exit_net - entry_commission), (
        f"the exit leg was booked {position.pnl} but the wallet moved "
        f"{_wallet_total(money):.8f} in total (entry {entry_commission:.8f} + "
        f"exit {exit_net:.8f}). A leg booked twice would make the gate "
        "over-trip; a leg booked zero times would make it blind."
    )


@pytest.mark.asyncio
async def test_the_forced_close_leg_is_booked_exactly_once(money):
    """The forced close does NOT go through `place_order`, so it books its own
    leg where its money moved. It must be booked exactly once.

    It books BEFORE the notifier, because the notifier flips the position to
    CLOSING and an exit leg is only booked while the position is OPEN.
    """
    controller, paper_executor, rm = _build_controller(ENTRY_PRICE)
    _pin_day(rm)
    position, _ = await _run_real_entry(controller, paper_executor)

    entry_commission = -float(position.pnl or 0.0)

    await paper_executor._close_position_at(
        SYMBOL,
        ENTRY_PRICE * 0.97,
        reason="LIQUIDATION",
        exit_type="STOP_LOSS",
    )

    wallet_total = _wallet_total(money)
    exit_net = wallet_total - (-entry_commission)

    assert float(position.pnl or 0.0) == pytest.approx(wallet_total), (
        f"after the forced close the position booked {position.pnl} but the "
        f"wallet moved {wallet_total:.8f} (entry {entry_commission:.8f} + exit "
        f"{exit_net:.8f}). The forced close must book its leg exactly once."
    )
    assert position.status == "CLOSING", (
        "the notifier is still what flips the position to CLOSING -- only the "
        "PnL booking moved out of it"
    )


# --- 3. A READ NEVER CREATES MONEY --------------------------------------------


@pytest.mark.asyncio
async def test_a_balance_read_never_creates_a_paper_wallet(monkeypatch):
    """With no wallet, the balance read must report NO balance -- not invent
    a $10,000 one.

    A $10,000 balance invented by a read is what the risk manager stamped as
    the day's opening balance, and 5% of $10,000 is $500 of tolerated loss
    against 5% of a real ~$756.28 base = $37.81.
    """
    created = []

    async def _fake_get_paper_wallet(db, user_id=None, *args, **kwargs):
        return []

    async def _fake_init_or_reset_paper_wallet(db, *args, **kwargs):
        created.append(kwargs)
        # The wallet a read would have invented, so the failure mode is
        # visible rather than silent.
        return [types.SimpleNamespace(asset="USDT", balance=10000.0)]

    monkeypatch.setattr(crud, "get_paper_wallet", _fake_get_paper_wallet)
    monkeypatch.setattr(
        crud, "init_or_reset_paper_wallet", _fake_init_or_reset_paper_wallet
    )

    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=_FakeDataConsumer(ENTRY_PRICE),
        redis_client=None,
    )

    balances = await paper_executor.get_account_balance()

    assert not created, (
        "a balance READ created a paper wallet -- a read must never create "
        "money. The risk manager would adopt that $10,000 as the day's "
        "opening balance and the daily-loss gate would be unable to trip."
    )
    assert balances is None, (
        f"with no wallet the balance must be reported as UNKNOWN, not as "
        f"{balances!r}. An invented balance becomes the daily-loss gate's "
        "denominator."
    )


@pytest.mark.asyncio
async def test_the_risk_manager_fails_closed_when_there_is_no_wallet(monkeypatch):
    """No wallet => no balance => no opening balance => the gate fails CLOSED.

    The failure must not be a $10,000 denominator: the gate has to say the
    opening balance is unknown.
    """
    from bot_module.risk_manager import RISK_GUARD_BASE_UNKNOWN_REASON

    created = []

    async def _fake_get_paper_wallet(db, user_id=None, *args, **kwargs):
        return []

    async def _fake_init_or_reset_paper_wallet(db, *args, **kwargs):
        created.append(kwargs)
        return [types.SimpleNamespace(asset="USDT", balance=10000.0)]

    monkeypatch.setattr(crud, "get_paper_wallet", _fake_get_paper_wallet)
    monkeypatch.setattr(
        crud, "init_or_reset_paper_wallet", _fake_init_or_reset_paper_wallet
    )

    paper_executor = PaperTradingExecutor(
        user_id=USER_ID,
        db_session=_FakeDB(),
        data_consumer=_FakeDataConsumer(ENTRY_PRICE),
        redis_client=None,
    )
    rm = RiskManager(
        executor=None,
        paper_executor=paper_executor,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    rm.redis_client = _FakeRedis()

    await rm.initialize_balance()

    assert not created, (
        "the risk manager's balance read created a paper wallet -- a read must "
        "never create money"
    )
    assert rm.stats.start_of_day_balance == 0.0, (
        "with no wallet there is no balance, so there is no opening balance: "
        f"got ${rm.stats.start_of_day_balance:.2f}. A $10,000 base is a "
        "13.2x disarm of the daily-loss gate."
    )
    assert rm.stats.start_of_day_day_str != _utc_day_str(), (
        "an opening balance that does not exist must not be stamped with "
        "today's day, or it would read as known"
    )
    assert rm._is_trading_allowed is False, (
        "an account with no wallet has no money to lose and no known opening "
        "balance, so the gate must fail closed"
    )
    assert rm.get_trading_disabled_reason(), (
        "the gate must publish a reason so an operator can see why trading is "
        "disabled"
    )
    assert RISK_GUARD_BASE_UNKNOWN_REASON in rm.get_trading_disabled_reason() or (
        "balance could not be fetched" in rm.get_trading_disabled_reason()
    ), (
        f"the reason must name the unknown balance, got "
        f"{rm.get_trading_disabled_reason()!r}"
    )


# --- 5. THE OPENING-BALANCE WRITER -------------------------------------------


@pytest.mark.asyncio
async def test_the_opening_balance_writer_never_overwrites_todays_base():
    """The ONE writer must refuse to overwrite a base that already belongs to
    today.

    Today's opening balance is a fact about the start of the day. Restating it
    from a mid-session balance would silently bake the losses already taken
    today into the denominator, which reads LOW and under-trips the gate.
    """
    rm = RiskManager(
        executor=_FakeLiveExecutor(),
        paper_executor=None,
        user_id=USER_ID,
        db_session=None,
        user_settings={},
    )
    today = _utc_day_str()
    rm.stats.start_of_day_balance = 500.0
    rm.stats.start_of_day_day_str = today

    wrote = rm._write_start_of_day_base(900.0, today)

    assert wrote is False, (
        "the writer overwrote a base that already belongs to today -- the "
        "day's opening balance is a fact about the start of the day, and "
        "restating it from a mid-session balance reads LOW"
    )
    assert rm.stats.start_of_day_balance == pytest.approx(500.0), (
        "the balance and its day stamp must be written together, and neither "
        "may be rewritten on its own"
    )
    assert rm.stats.start_of_day_day_str == today


# --- 4. THE API: A READ NEVER CREATES MONEY -----------------------------------


@pytest.mark.asyncio
async def test_the_paper_account_get_never_creates_a_wallet(
    pro_user_client, pro_user, db_session
):
    """`GET /api/v1/account/paper` must not create a wallet.

    It used to create a $10,000 wallet for an account that had none. That
    $10,000 is what the risk manager stamped as the day's opening balance,
    and 5% of $10,000 is $500 of tolerated loss against 5% of a real ~$756.28
    base = $37.81 -- a 13.2x disarm of the daily-loss gate.
    """
    from api import models
    from sqlalchemy import delete, select

    async def _wallet_rows():
        result = await db_session.execute(
            select(models.PaperWallet).where(
                models.PaperWallet.user_id == pro_user.id
            )
        )
        return list(result.scalars().all())

    await db_session.execute(
        delete(models.PaperWallet).where(models.PaperWallet.user_id == pro_user.id)
    )
    await db_session.commit()
    assert await _wallet_rows() == [], "the test needs an account with no wallet"

    response = await pro_user_client.get("/api/v1/account/paper")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == [], (
        "the GET reported a wallet for an account that has none, so it "
        "created one -- a read must never create money"
    )

    rows = await _wallet_rows()
    assert rows == [], (
        "the GET created a paper wallet -- a read must never create money. "
        "The risk manager would adopt that $10,000 as the day's opening "
        "balance and the daily-loss gate would be unable to trip."
    )


@pytest.mark.asyncio
async def test_the_paper_portfolio_get_never_creates_a_wallet(
    pro_user_client, pro_user, db_session
):
    """`GET /api/v1/portfolio?mode=paper` must not create a wallet either."""
    from api import models
    from sqlalchemy import delete, select

    async def _wallet_rows():
        result = await db_session.execute(
            select(models.PaperWallet).where(
                models.PaperWallet.user_id == pro_user.id
            )
        )
        return list(result.scalars().all())

    await db_session.execute(
        delete(models.PaperWallet).where(models.PaperWallet.user_id == pro_user.id)
    )
    await db_session.commit()
    assert await _wallet_rows() == [], "the test needs an account with no wallet"

    response = await pro_user_client.get("/api/v1/portfolio?mode=paper")
    assert response.status_code == 200, response.text

    rows = await _wallet_rows()
    assert rows == [], (
        "the portfolio GET created a paper wallet -- a read must never create "
        "money"
    )


@pytest.mark.asyncio
async def test_the_destructive_paper_wallet_reset_requires_an_explicit_opt_in(
    db_session, pro_user
):
    """Only the explicit reset may destroy a live wallet.

    `init_or_reset_paper_wallet` used to delete the existing wallet rows on
    EVERY call. It now defaults to create-if-absent, and the destructive
    half requires `reset=True`, which only the explicit
    `POST /api/v1/account/paper/reset` passes.
    """
    from api import crud as api_crud
    from api import models
    from sqlalchemy import select

    # A wallet with a balance that is unmistakably not the initial balance.
    await api_crud.update_paper_wallet_balance(
        db_session, pro_user.id, "USDT", 123.45
    )
    await db_session.commit()

    async def _balance():
        result = await db_session.execute(
            select(models.PaperWallet).where(
                models.PaperWallet.user_id == pro_user.id
            )
        )
        rows = list(result.scalars().all())
        return rows[0].balance if rows else None

    assert await _balance() == pytest.approx(123.45)

    # The default: create-if-absent. An existing wallet is left alone.
    await api_crud.init_or_reset_paper_wallet(db_session, user_id=pro_user.id)
    await db_session.commit()
    assert await _balance() == pytest.approx(123.45), (
        "the default path re-seeded or deleted a live wallet -- only an "
        "explicit reset may do that"
    )

    # The explicit opt-in: the destructive half.
    await api_crud.init_or_reset_paper_wallet(
        db_session, user_id=pro_user.id, reset=True
    )
    await db_session.commit()
    assert await _balance() == pytest.approx(10000.0), (
        "the explicit reset must still reset the wallet to the initial balance"
    )
