"""The published PnL of a strategy must be TRUE.

THE DEFECT
----------
`TradingController._publish_state_to_redis()` asked the RiskManager for
the realized PnL of every running strategy:

    realized_pnl = self.rm.get_pnl_for_strategy(
        symbol=None, strategy_name=instance.NAME
    )

Two things are wrong with that call, and together they made the strategy
card show `pnl = 0.0` for a strategy that had lost thousands of dollars:

1. The RiskManager's per-strategy ledger is keyed `(symbol, strategy_name)`.
   The call passed `symbol=None`, so it looked up the key
   `(None, "VisualBuilderStrategy")` -- a key that can never exist, because
   the ledger is only ever written with a real symbol. The lookup therefore
   returned 0.0 for EVERY strategy, in EVERY mode. (Verified: the ledger is
   a `defaultdict`, but `get_pnl_for_strategy` reads it with `.get()`, which
   does not create a default.)

2. `instance.NAME` is the strategy CLASS name, not the strategy's identity.
   Every visual-builder strategy is called "VisualBuilderStrategy", so the
   question was unanswerable: it could not tell two strategies apart, and
   it carried no trading mode, so paper and live shared one bucket.

MEASURED ON PRODUCTION, 2026-10-25 (user 1, `GET /api/v1/strategies?mode=paper`)
-----------------------------------------------------------------------------
`paper-bollinger-1` (e43d1cb2-123c-4ca7-ba4f-bb25d245e969) and
`paper-test-2` (be485ab9-384a-4adb-9f9b-a2df412d6db0) were both published
with `pnl=0.0`. The trades table holds 282 paper rows for the first
strategy, and the money says (re-measured at full precision):

    the 89 rows with is_final_exit == true   net  -5689.1548615014922809
    the 90 rows labelled exit_type FINAL*    net  -5690.8643461715722809
    every one of the 282 rows                net  -9356.1794634944660301

The first two differ by exactly ONE row: trade id 941, whose
`exit_type` says the position closed finally while its `is_final_exit`
flag says it did not (pnl 0.0, commission 1.70948467008). That
disagreement is a data defect of its own; this PR does not fix it, it
publishes the truth on both sides of it.

`paper-test-2` has no trades at all, and 0.0 is the CORRECT number for it:
its entry condition is `rsi_condition(value=55, period=14,
operator=cross_above)` and RSI is already 57.28, so the cross cannot fire
until RSI first drops below 55.

WHICH FLAG IS AUTHORITATIVE
---------------------------
`is_final_exit`, and the reason is structural rather than a preference:
the LIVE trade row sets `is_final_exit = True` and leaves `exit_type`
NULL (bot_module/controller.py), while only the paper executor writes
`exit_type` at all. A definition that reads `exit_type` therefore
computes NOTHING for live mode. `is_final_exit` is the DB's boolean
statement of "this row closed the position", it is written on every
path, and it is what the aggregate filters on.

WHAT THIS TEST FILE PROVES
--------------------------
1. The published PnL of a strategy with finalized paper trades is the
   TRUE realized net -- asserted against the exact production figures.
2. Two different config ids never share a number, even when their strategy
   CLASS name is identical (the actual production situation).
3. Paper and live never mix: the same config id traded in both books gets
   two different numbers.
4. The publisher issues O(1) queries per cycle, not O(N) per strategy.
5. A strategy with no trades publishes 0.0 -- and 0.0 means "no trades",
   never "the database was unreachable".

Red-then-green: on `origin/main` the published PnL is 0.0 and every
assertion about the realized net fails.
"""

import asyncio
import json
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api import models
from bot_module import config as bot_config
from bot_module.controller import ActivePositionMap, TradingController


USER_ID = 1

# The two strategies production was running on 2026-10-25.
PAPER_BOLLINGER = "e43d1cb2-123c-4ca7-ba4f-bb25d245e969"
PAPER_TEST_2 = "be485ab9-384a-4adb-9f9b-a2df412d6db0"

# The REAL production figures for paper-bollinger-1, measured from
# `GET /api/v1/trades?mode=paper&limit=10000` and re-measured at full
# precision.
#
# The three totals below are the authoritative ones, and they are pinned
# exactly (not "approximately -5690"):
#
#   SUM(pnl - commission) over the 89 rows with is_final_exit IS TRUE
#     = -5689.1548615014922809      <- what this PR publishes as `pnl`
#   SUM(pnl - commission) over the 90 rows whose exit_type starts FINAL
#     = -5690.8643461715722809      <- the same book, read by its label
#   SUM(pnl - commission) over all 282 rows
#     = -9356.1794634944660301      <- the wallet's cash movement
#
# The first two differ by exactly one row -- trade id 941 -- whose
# `exit_type` says the position closed finally and whose `is_final_exit`
# says it did not, and which cost the wallet 1.70948467008 of commission.
#
# The seed below reproduces all three totals EXACTLY. The per-group
# figures are the measured ones, rounded to 4 decimals by the measurement
# itself; the 1.73e-05 that makes the totals add up is carried by the
# ENTRY group, where it is invisible (1e-7 on each of 169 commissions).
PRODUCTION_ROWS = [
    # exit_type,            count, total pnl,     total NET,   finalized rows
    ("ENTRY", 169, 0.0000, -2474.1759173228937492, 0),
    ("FINAL_STOP_LOSS", 73, -3889.5457, -5596.9938, 73),
    ("FINAL_EXIT", 11, -16.5832, -44.4864615014922809, 11),
    # Trade id 941: the position closed (its exit_type says so) but the
    # boolean says it did not. It moved no PnL and cost 1.70948467008.
    ("FINAL_EXIT", 1, 0.0000, -1.70948467008, 0),
    ("FINAL_TAKE_PROFIT", 5, 60.0513, -47.6746, 5),
    ("PARTIAL_STOP_LOSS", 20, -874.7388, -1326.4864, 0),
    ("PARTIAL_TAKE_PROFIT", 3, 166.4835, 135.3472, 0),
]
PRODUCTION_TOTAL_ROWS = sum(r[1] for r in PRODUCTION_ROWS)  # 282
PRODUCTION_FINALIZED_ROWS = 89
PRODUCTION_TOTAL_PNL = -4554.3329
# The 89 rows with is_final_exit IS TRUE -- the number this PR publishes.
PRODUCTION_REALIZED_NET = -5689.1548615014922809
# The same book read by its exit_type label (90 rows).
PRODUCTION_FINAL_LABELLED_NET = -5690.8643461715722809
# Every row: the wallet's cash movement.
PRODUCTION_NET_CASH_FLOW = -9356.1794634944660301
# The one row on which the two flags disagree.
PRODUCTION_ROW_941_NET = -1.70948467008
PRODUCTION_ROW_941_COMMISSION = 1.70948467008


# --- The doubles ------------------------------------------------------------


class _FakePipeline:
    """Captures what the publisher writes, instead of writing it to Redis."""

    def __init__(self, sink):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def set(self, key, value):
        self._sink[key] = value

    def publish(self, channel, message):
        pass

    async def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakePipeline(self.store)

    async def get(self, key):
        return self.store.get(key)


class _FakeRiskManager:
    """Only the surface the publisher reads."""

    def __init__(self):
        self.stats = types.SimpleNamespace(
            current_balance=1000.0, today_pnl=0.0, consecutive_losses=0
        )
        self._is_trading_allowed = True

    def get_trading_disabled_reason(self):
        return None

    def get_pnl_for_strategy(self, symbol=None, strategy_name=None):
        """The real ledger, faithfully: keyed (symbol, strategy_name).

        The publisher used to ask it for `(None, "VisualBuilderStrategy")`,
        a key that can never exist, so it always answered 0.0. This stub
        answers 0.0 too -- but the tests must catch the CALL, not the
        number it returns.
        """
        return 0.0


class _CountingSession:
    """A DB session that COUNTS the queries the product code runs.

    The publisher must not issue one query per strategy, so the test needs
    to see the real number of statements that reach the database.
    """

    def __init__(self, session, counter):
        self._session = session
        self._counter = counter

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def execute(self, *args, **kwargs):
        self._counter["queries"] += 1
        return await self._session.execute(*args, **kwargs)


def _split(total: float, count: int):
    """`count` numbers that sum to `total`.

    The split keeps ten decimals -- production rows carry full precision,
    and rounding them to four would destroy the exactness the measured
    totals are pinned at. The last number takes the residue, so the group
    sum is the total.
    """
    if count <= 0:
        return []
    base = round(total / count, 10)
    parts = [base] * (count - 1)
    parts.append(total - sum(parts))
    return parts


async def _seed_trade_rows(
    db,
    *,
    config_id: str,
    mode: str,
    rows,
    user_id: int = USER_ID,
    symbol: str = "ETHUSDT",
):
    """Writes trade rows that look like the ones production holds.

    `rows` is a list of (exit_type, count, total_pnl, total_net, finalized):
    the commission of a row is `pnl - net`, so the group totals of pnl, of
    commission and of net are all exactly what the caller asked for.

    A group that has final exits carries its whole total on those final
    rows; a group that has none (the entries, the partial exits) carries
    its total on all of its rows. A row that is neither final nor
    net-bearing is a row that moved no money.
    """
    now = datetime.now(timezone.utc)
    offset = 0
    for exit_type, count, total_pnl, total_net, finalized in rows:
        net_bearing = finalized if finalized > 0 else count
        pnls = _split(total_pnl, net_bearing)
        nets = _split(total_net, net_bearing)
        for i in range(count):
            offset += 1
            if i < net_bearing:
                row_pnl = pnls[i]
                row_net = nets[i]
            else:
                row_pnl = 0.0
                row_net = 0.0
            db.add(
                models.Trade(
                    user_id=user_id,
                    trade_uuid=f"seed-{config_id}-{mode}-{offset}-{uuid.uuid4().hex[:8]}",
                    timestamp_close=now - timedelta(minutes=offset),
                    timestamp_signal=now - timedelta(minutes=offset + 5),
                    symbol=symbol,
                    strategy_config_id=config_id,
                    direction="BUY",
                    entry_price=100.0,
                    exit_price=95.0,
                    pnl=row_pnl,
                    commission=row_pnl - row_net,
                    exit_reason="SEEDED",
                    quantity=1.0,
                    trade_mode=mode,
                    exit_type=exit_type,
                    is_final_exit=i < finalized,
                    position_entry_id=f"x-entry-seed-{offset}",
                )
            )
    await db.commit()


async def _seed_strategy_config(db, config_id: str, name: str, user_id: int = USER_ID):
    db.add(
        models.StrategyConfig(
            id=config_id,
            name=name,
            user_id=user_id,
            config_data={"params": {}, "marketType": "PAPER"},
            symbol_selection_mode="STATIC",
            symbols=["ETHUSDT"],
        )
    )
    await db.commit()


def _build_controller(db_session, counter, strategies):
    """A controller carrying only the surface the publisher touches.

    Built with `__new__` because `TradingController.__init__` opens exchange
    connections; every method used below is the real, unbound production one.
    """
    redis_client = _FakeRedis()

    controller = TradingController.__new__(TradingController)
    controller.loop = asyncio.get_event_loop()
    controller.user_id = USER_ID
    controller.api_key_id = None
    controller.api_key_name = "paper"
    controller.executors = {}
    controller.market_executors = {}
    controller.consumer = None
    controller.redis_client = redis_client
    controller._positions_dict_lock = asyncio.Lock()
    controller._active_positions = ActivePositionMap()
    controller.instances_lock = asyncio.Lock()
    controller.running_strategy_instances = strategies
    controller.rm = _FakeRiskManager()

    async def _get_db_session():
        yield _CountingSession(db_session, counter)

    controller.get_db_session = _get_db_session
    return controller, redis_client


def _strategy_instance(class_name: str = "VisualBuilderStrategy"):
    """A strategy instance: the publisher only reads its CLASS name."""
    return types.SimpleNamespace(NAME=class_name)


def _published_strategies(redis_client) -> dict:
    """The strategies the publisher wrote, keyed by config id."""
    key = f"{bot_config.REDIS_STATE_KEY_STRATEGIES}:{USER_ID}:None"
    payload = json.loads(redis_client.store[key])
    return {str(s["id"]): s for s in payload}


async def _publish(controller, redis_client):
    await controller._publish_state_to_redis()
    return _published_strategies(redis_client)


# --- 1. The published PnL is the TRUE realized net ----------------------------


@pytest.mark.asyncio
async def test_published_pnl_is_the_true_realized_net_of_the_production_strategy(db_session):
    """paper-bollinger-1 must publish -5689.1549, not 0.0.

    This is the production book, row for row: 282 paper rows, 89 of them
    with `is_final_exit IS TRUE`, reproducing all three measured totals
    exactly.
    """
    await _seed_strategy_config(db_session, PAPER_BOLLINGER, "paper-bollinger-1")
    await _seed_trade_rows(
        db_session,
        config_id=PAPER_BOLLINGER,
        mode="PAPER",
        rows=PRODUCTION_ROWS,
    )
    assert PRODUCTION_TOTAL_ROWS == 282

    # The seeded book reproduces the measured production totals EXACTLY, so
    # the assertions below are about the PUBLISHER, not about the seed.
    seeded = (
        await db_session.execute(
            select(
                models.Trade.pnl,
                models.Trade.commission,
                models.Trade.is_final_exit,
                models.Trade.exit_type,
            ).where(models.Trade.strategy_config_id == PAPER_BOLLINGER)
        )
    ).all()
    assert len(seeded) == PRODUCTION_TOTAL_ROWS
    assert sum(r.pnl for r in seeded) == pytest.approx(
        PRODUCTION_TOTAL_PNL, abs=1e-6
    )
    assert sum(1 for r in seeded if r.is_final_exit) == PRODUCTION_FINALIZED_ROWS
    assert sum(r.pnl - r.commission for r in seeded) == pytest.approx(
        PRODUCTION_NET_CASH_FLOW, abs=1e-9
    ), "the seeded book must reconcile with the wallet to the cent"
    assert sum(
        r.pnl - r.commission for r in seeded if r.is_final_exit
    ) == pytest.approx(PRODUCTION_REALIZED_NET, abs=1e-9)
    assert sum(
        r.pnl - r.commission for r in seeded if (r.exit_type or "").startswith("FINAL")
    ) == pytest.approx(PRODUCTION_FINAL_LABELLED_NET, abs=1e-9)

    # The one row on which the two flags disagree is in the book, with the
    # measured values.
    row_941 = [r for r in seeded if not r.is_final_exit and r.exit_type == "FINAL_EXIT"]
    assert len(row_941) == 1
    assert row_941[0].pnl == 0.0
    assert row_941[0].commission == pytest.approx(
        PRODUCTION_ROW_941_COMMISSION, abs=1e-12
    )

    counter = {"queries": 0}
    strategies = {
        PAPER_BOLLINGER: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    published = await _publish(controller, redis_client)
    card = published[PAPER_BOLLINGER]

    # The published PnL is rounded to 4 decimals, like every other number
    # the publisher writes. The EXACT value it comes from is pinned below.
    assert card["pnl"] == round(PRODUCTION_REALIZED_NET, 4) == -5689.1549, (
        f"the strategy card must show the money it actually lost, got {card['pnl']!r}"
    )
    assert card["realized_pnl"] == round(PRODUCTION_REALIZED_NET, 4)
    assert card["trade_count"] == PRODUCTION_FINALIZED_ROWS, (
        f"89 finalized trades produced that number, got {card['trade_count']!r}"
    )
    # The wallet moved more than the strategy realized: the difference is
    # the entry commission of positions that were opened and never closed,
    # plus the one row whose exit_type and is_final_exit disagree.
    assert card["net_cash_flow"] == round(PRODUCTION_NET_CASH_FLOW, 4) == -9356.1795

    # And the raw aggregate, before rounding, is the measured value to the
    # last digit -- not "approximately -5690".
    from api import crud as api_crud

    raw = await api_crud.get_strategy_realized_pnl_aggregates(
        db_session, user_id=USER_ID, strategy_config_ids=[PAPER_BOLLINGER]
    )
    assert raw[(PAPER_BOLLINGER, "PAPER")]["realized_net"] == pytest.approx(
        PRODUCTION_REALIZED_NET, abs=1e-9
    )
    assert raw[(PAPER_BOLLINGER, "PAPER")]["net_cash_flow"] == pytest.approx(
        PRODUCTION_NET_CASH_FLOW, abs=1e-9
    )
    assert raw[(PAPER_BOLLINGER, "PAPER")]["finalized_count"] == 89


@pytest.mark.asyncio
async def test_a_strategy_with_no_trades_publishes_zero(db_session):
    """paper-test-2 has never traded, so 0.0 is the correct number for it.

    Its entry condition cannot fire (RSI 57.28, cross_above 55), so it has
    no rows at all. Zero trades must read as zero PnL -- and zero trades
    is not the same as "the database was unreachable".
    """
    await _seed_strategy_config(db_session, PAPER_TEST_2, "paper-test-2")

    counter = {"queries": 0}
    strategies = {
        PAPER_TEST_2: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    published = await _publish(controller, redis_client)
    card = published[PAPER_TEST_2]

    assert card["pnl"] == pytest.approx(0.0, abs=1e-9)
    assert card["realized_pnl"] == pytest.approx(0.0, abs=1e-9)
    assert card["trade_count"] == 0


@pytest.mark.asyncio
async def test_the_pnl_is_not_read_from_the_risk_manager_ledger(db_session):
    """The published number must not come from the RiskManager's ledger.

    That ledger is keyed `(symbol, strategy_name)`: it cannot answer "the
    PnL of THIS strategy", and the call that used to ask it passed
    `symbol=None`, so it always answered 0.0. It must not be consulted.
    """
    await _seed_strategy_config(db_session, PAPER_BOLLINGER, "paper-bollinger-1")
    await _seed_trade_rows(
        db_session,
        config_id=PAPER_BOLLINGER,
        mode="PAPER",
        rows=[("FINAL_STOP_LOSS", 1, -100.0, -110.0, 1)],
    )

    counter = {"queries": 0}
    strategies = {
        PAPER_BOLLINGER: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        return 0.0

    controller.rm.get_pnl_for_strategy = _forbidden

    published = await _publish(controller, redis_client)
    card = published[PAPER_BOLLINGER]

    assert not calls, (
        "the RiskManager ledger must not be consulted for a per-strategy "
        f"PnL, but it was called {len(calls)} time(s)"
    )
    assert card["pnl"] == pytest.approx(-110.0, abs=1e-6)


# --- 2. Two strategies never share a number -----------------------------------


@pytest.mark.asyncio
async def test_two_config_ids_never_share_a_pnl_number(db_session):
    """Two visual-builder strategies must not share one PnL.

    Both instances are called "VisualBuilderStrategy" -- that is the
    production situation, and it is exactly why the class name cannot be
    the key.
    """
    first = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    second = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    await _seed_strategy_config(db_session, first, "strategy-one")
    await _seed_strategy_config(db_session, second, "strategy-two")

    await _seed_trade_rows(
        db_session,
        config_id=first,
        mode="PAPER",
        rows=[("FINAL_TAKE_PROFIT", 2, 250.0, 230.0, 2)],
    )
    await _seed_trade_rows(
        db_session,
        config_id=second,
        mode="PAPER",
        rows=[("FINAL_STOP_LOSS", 3, -600.0, -660.0, 3)],
    )

    counter = {"queries": 0}
    strategies = {
        first: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        ),
        second: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        ),
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    published = await _publish(controller, redis_client)

    assert published[first]["pnl"] == pytest.approx(230.0, abs=1e-6), (
        "the winning strategy must publish its own result"
    )
    assert published[second]["pnl"] == pytest.approx(-660.0, abs=1e-6), (
        "the losing strategy must publish its own result, not the winner's"
    )
    assert published[first]["pnl"] != published[second]["pnl"]
    assert published[first]["trade_count"] == 2
    assert published[second]["trade_count"] == 3


# --- 3. Paper and live never mix ---------------------------------------------


@pytest.mark.asyncio
async def test_paper_and_live_never_mix(db_session):
    """The same config id in both books gets two different numbers."""
    config_id = "cccccccc-3333-4333-8333-cccccccccccc"
    await _seed_strategy_config(db_session, config_id, "dual-mode")

    await _seed_trade_rows(
        db_session,
        config_id=config_id,
        mode="PAPER",
        rows=[("FINAL_STOP_LOSS", 4, -800.0, -880.0, 4)],
    )
    await _seed_trade_rows(
        db_session,
        config_id=config_id,
        mode="LIVE",
        rows=[("FINAL_TAKE_PROFIT", 4, 400.0, 360.0, 4)],
    )

    counter = {"queries": 0}
    strategies = {
        config_id: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    published = await _publish(controller, redis_client)
    card = published[config_id]

    assert card["pnl"] == pytest.approx(-880.0, abs=1e-6), (
        "the paper book must show the paper result, not the paper+live sum"
    )
    assert card["trade_count"] == 4

    # The same config id, published as a LIVE strategy, must show the live
    # result. One config id, two books, two numbers.
    strategies[config_id] = (
        _strategy_instance(),
        {"mode": "live", "user_id": USER_ID, "config_data": {}},
    )
    controller, redis_client = _build_controller(db_session, counter, strategies)
    published = await _publish(controller, redis_client)
    card = published[config_id]

    assert card["pnl"] == pytest.approx(360.0, abs=1e-6), (
        "the live book must show the live result, not the paper+live sum"
    )
    assert card["trade_count"] == 4


@pytest.mark.asyncio
async def test_one_users_trades_never_leak_into_another_users_strategy(db_session):
    """A strategy only ever reports the trades of its own owner."""
    config_id = "dddddddd-4444-4444-8444-dddddddddddd"
    await _seed_strategy_config(db_session, config_id, "mine")

    await _seed_trade_rows(
        db_session,
        config_id=config_id,
        mode="PAPER",
        rows=[("FINAL_STOP_LOSS", 2, -500.0, -550.0, 2)],
    )
    # Somebody else's book.
    await _seed_trade_rows(
        db_session,
        config_id=config_id,
        mode="PAPER",
        rows=[("FINAL_TAKE_PROFIT", 7, 7000.0, 6300.0, 7)],
        user_id=2,
    )

    counter = {"queries": 0}
    strategies = {
        config_id: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    published = await _publish(controller, redis_client)
    card = published[config_id]

    assert card["pnl"] == pytest.approx(-550.0, abs=1e-6), (
        "another user's trades must not move this strategy's PnL"
    )
    assert card["trade_count"] == 2


# --- 4. The query count is bounded -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_count", [1, 5, 25])
async def test_the_publisher_issues_one_aggregate_query_per_cycle(
    db_session, strategy_count
):
    """N strategies must cost O(1) queries, not O(N).

    The publisher runs on a 2-second loop, so a per-strategy query would
    be a per-strategy query every 2 seconds, for every controller.
    """
    strategies = {}
    for i in range(strategy_count):
        config_id = f"{i:08d}-0000-4000-8000-000000000000"
        await _seed_strategy_config(db_session, config_id, f"strategy-{i}")
        await _seed_trade_rows(
            db_session,
            config_id=config_id,
            mode="PAPER",
            rows=[("FINAL_STOP_LOSS", 2, -200.0 - i, -220.0 - i, 2)],
        )
        strategies[config_id] = (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )

    counter = {"queries": 0}
    controller, redis_client = _build_controller(db_session, counter, strategies)

    published = await _publish(controller, redis_client)

    assert len(published) == strategy_count
    assert counter["queries"] == 1, (
        f"{strategy_count} strategies must cost ONE aggregate query, not "
        f"{counter['queries']}"
    )

    # And the numbers are still per strategy.
    for i, config_id in enumerate(strategies):
        assert published[config_id]["pnl"] == pytest.approx(
            -220.0 - i, abs=1e-6
        )


# --- 5. An unreachable database is "unknown", not 0.0 -------------------------


@pytest.mark.asyncio
async def test_an_unreachable_database_is_reported_as_unknown_not_as_zero(db_session):
    """A false 0.0 is the bug this whole fix exists to remove.

    When the numbers cannot be read, the published PnL must be null
    ("unknown"), never 0.0 ("broke even").
    """
    await _seed_strategy_config(db_session, PAPER_BOLLINGER, "paper-bollinger-1")
    await _seed_trade_rows(
        db_session,
        config_id=PAPER_BOLLINGER,
        mode="PAPER",
        rows=[("FINAL_STOP_LOSS", 1, -100.0, -110.0, 1)],
    )

    counter = {"queries": 0}
    strategies = {
        PAPER_BOLLINGER: (
            _strategy_instance(),
            {"mode": "paper", "user_id": USER_ID, "config_data": {}},
        )
    }
    controller, redis_client = _build_controller(db_session, counter, strategies)

    async def _broken_get_db_session():
        raise RuntimeError("the database is on fire")
        yield  # pragma: no cover -- makes this an async generator

    controller.get_db_session = _broken_get_db_session

    published = await _publish(controller, redis_client)
    card = published[PAPER_BOLLINGER]

    assert card["pnl"] is None, (
        "an unreadable PnL must be published as unknown, not as 0.0"
    )
    assert card["realized_pnl"] is None
    assert card["trade_count"] is None
