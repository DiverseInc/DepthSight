# tests/test_controller_db_session_leak.py
# ruff: noqa: E402
"""The controller must not abandon a DB session.

`async for db in self.get_db_session():` walks an async generator. When the
body returns - or raises - the generator is left suspended at its `yield`, so
the `async with AsyncSessionLocal() as session:` inside `api.database.get_db`
never exits: the session keeps its pool connection checked out and its
transaction OPEN until the garbage collector finalises the generator. That is
exactly the mechanism that cannot be relied on at fleet scale. On the
2026-10-19 night run it was ~120 pinned server connections at 660
controllers: the pooler ran out of server connections, 1300 clients queued,
and every query past them timed out.

These tests observe the CONNECTION, not the syntax. A fake session factory
mirrors `api.database.get_db` (check the connection out when the session is
created, return it when the session's exit path runs) and the test asserts
that nothing is still checked out once the controller is done with it.

The fake factory keeps every generator it hands out in `self.generators` ON
PURPOSE. CPython does eventually finalise an abandoned async generator, so a
test that let the GC do the cleanup would pass on the broken code and prove
nothing. Pinning the generator makes the assertion about the DETERMINISTIC
release - the property the fix actually provides, and the one production
depends on.

`test_no_raw_async_for_over_get_db_session` is the cheap structural net
(upstream's own regression test) that also covers the three sites buried too
deep in the signal/final-exit paths to unit test in isolation.
"""

import ast
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("POSTGRES_USER", "testuser")
os.environ.setdefault("POSTGRES_PASSWORD", "testpassword")
os.environ.setdefault("POSTGRES_DB", "testdb")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_module import controller as controller_mod
from bot_module.controller import TradingController
from bot_module.rehydrate import refresh_payload_config_from_db
from bot_module.runtime_dependencies import crud
from bot_module.symbol_selection import SymbolSelectionConfig
from bot_module.telegram_notifier import TelegramNotifier

# The names a session factory is called by. `get_db_session` is the
# controller's own (`self.get_db_session()`); `db_session_factory` is the
# rehydrate path's, and it is called as a BARE NAME (it is a parameter), so
# the guard has to match a plain name as well as an attribute.
_SESSION_FACTORY_NAMES = {"get_db_session", "db_session_factory"}


# ---------------------------------------------------------------------------
# The pool, reduced to a checked-out counter
# ---------------------------------------------------------------------------


class _Pool:
    """The server-side pool, reduced to what the leak is measured in."""

    def __init__(self):
        self.checked_out = 0
        self.opened = 0

    def acquire(self):
        self.checked_out += 1
        self.opened += 1

    def release(self):
        self.checked_out -= 1
        assert self.checked_out >= 0, "released a connection that was not held"


class _Session:
    """The slice of AsyncSession the controller's DB paths touch."""

    def __init__(self, pool):
        self._pool = pool
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        if not self.closed:
            self.closed = True
            self._pool.release()

    async def __aenter__(self):
        # The real `AsyncSession` is an async context manager, and the
        # Telegram path takes the session out of the generator and then uses
        # `async with` on it.
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False


class _DbFactory:
    """A `get_db`-shaped session factory.

    `api.database.get_db` is

        async def get_db():
            async with AsyncSessionLocal() as session:
                yield session

    so the connection is checked out when the generator body starts and is
    returned only when that `async with` exits. This fake does exactly that.
    """

    def __init__(self):
        self.pool = _Pool()
        self.generators = []
        self.sessions = []

    def __call__(self):
        gen = self._get_db()
        # Held on purpose: see the module docstring.
        self.generators.append(gen)
        return gen

    async def _get_db(self):
        self.pool.acquire()
        session = _Session(self.pool)
        self.sessions.append(session)
        try:
            yield session
        finally:
            # `async with AsyncSessionLocal() as session:` -> __aexit__ -> close
            await session.close()


def _controller(factory):
    """A controller carrying only the attributes the DB paths touch.

    Built with `__new__` because `TradingController.__init__` opens exchange
    connections; every method used below is the real, unbound production one.
    """
    ctrl = TradingController.__new__(TradingController)
    ctrl.get_db_session = factory
    ctrl.user_id = 1
    ctrl.api_key_id = None
    ctrl.api_key_name = "paper"
    ctrl.symbol_selection_config = SymbolSelectionConfig()
    ctrl.user_telegram_chat_id = None
    ctrl.rm = None
    return ctrl


# ---------------------------------------------------------------------------
# The leak
# ---------------------------------------------------------------------------


async def test_a_read_leaves_no_connection_checked_out(monkeypatch):
    """The happy path must give the connection back, not just the error path."""

    async def _fake_get_user_symbol_selection_config(db, user_id):
        return None

    monkeypatch.setattr(
        crud, "get_user_symbol_selection_config", _fake_get_user_symbol_selection_config
    )

    factory = _DbFactory()
    ctrl = _controller(factory)

    assert await ctrl.load_symbol_selection_config() is False

    assert factory.pool.opened == 1, "the controller did not open a session"
    assert factory.pool.checked_out == 0, (
        "the controller left a pooled connection checked out: the session's "
        "exit path never ran"
    )


async def test_a_failed_read_still_releases_the_connection(monkeypatch):
    """A leak on the ERROR path is worse than on the happy path.

    The read raises (DB pressure, a timeout, a constraint): the controller
    swallows it and carries on, but the session it was holding must still go
    back to the pool.
    """

    async def _boom(db, user_id):
        raise RuntimeError("db is down")

    monkeypatch.setattr(crud, "get_user_symbol_selection_config", _boom)

    factory = _DbFactory()
    ctrl = _controller(factory)

    assert await ctrl.load_symbol_selection_config() is False

    assert factory.pool.checked_out == 0, (
        "a failed read left a pooled connection checked out"
    )


async def test_the_adopted_position_lookup_releases_the_connection(monkeypatch):
    """The lookup that `break`s out of the session loop must release it too.

    The `break` is the interesting part: it leaves the session generator
    suspended at its `yield`, exactly like an exception would.
    """

    async def _fake_get_last_open_trade_for_symbol(db, user_id, symbol):
        return SimpleNamespace(
            strategy_config=SimpleNamespace(name="VolumeBreakout"),
            strategy_config_id=7,
            trade_uuid="x-entry-abc",
        )

    monkeypatch.setattr(
        crud, "get_last_open_trade_for_symbol", _fake_get_last_open_trade_for_symbol
    )

    factory = _DbFactory()
    ctrl = _controller(factory)

    executor = MagicMock()
    executor.get_open_orders = AsyncMock(return_value=[])

    position = await ctrl._build_adopted_position(
        "BTCUSDT",
        {"positionAmt": "1", "entryPrice": "100"},
        "futures_usdtm",
        executor,
    )

    assert position is not None
    assert factory.pool.checked_out == 0, (
        "the adopted-position lookup left a pooled connection checked out"
    )


async def test_many_operations_leave_no_connection_pinned(monkeypatch):
    """After N controller operations the number of open connections is bounded.

    This is the fleet-scale shape of the defect: one controller operation
    every few seconds, hundreds of controllers, and a pool that fills up
    because nobody gave the connections back.
    """

    async def _fake_get_user_symbol_selection_config(db, user_id):
        return None

    async def _fake_get_config(db, user_id=None):
        return None

    monkeypatch.setattr(
        crud, "get_user_symbol_selection_config", _fake_get_user_symbol_selection_config
    )
    monkeypatch.setattr(crud, "get_config", _fake_get_config)

    factory = _DbFactory()
    ctrl = _controller(factory)

    operations = 50
    for _ in range(operations):
        await ctrl.load_symbol_selection_config()
        await ctrl.reload_user_app_config()

    assert factory.pool.opened == operations * 2
    assert factory.pool.checked_out == 0, (
        f"{factory.pool.checked_out} connections were still checked out after "
        f"{operations} controller operations"
    )


async def test_the_rehydrate_lookup_releases_the_connection(monkeypatch):
    """The rehydrate path pins a connection the same way the controller did.

    `refresh_payload_config_from_db` is called once per strategy on every
    bot restart, with the controller's own session factory
    (`controller.py:3993`). It used to walk that factory with
    `async for ... : break`, abandoning the generator - so every restart
    rehydrating every strategy left a connection pinned until the GC.
    """

    async def _fake_get_strategy_config(db, user_id, config_id):
        return None

    monkeypatch.setattr(crud, "get_strategy_config", _fake_get_strategy_config)

    factory = _DbFactory()
    payload = {"id": "cfg-1", "config_data": {"symbol": "BTCUSDT"}}

    returned, refreshed = await refresh_payload_config_from_db(payload, 1, factory)

    assert refreshed is False
    assert factory.pool.opened == 1, "the rehydrate path did not open a session"
    assert factory.pool.checked_out == 0, (
        "the rehydrate lookup left a pooled connection checked out"
    )


async def test_the_telegram_link_closes_its_session_generator(monkeypatch):
    """The Telegram link handler took a session out of the generator.

    `async with await anext(db_gen) as db:` closes the SESSION. It does NOT
    close the GENERATOR: the generator is left suspended at its `yield`,
    inside the `async with AsyncSessionLocal()` that owns the pooled
    connection. Only `db_gen.aclose()` ends that `async with` for good.

    The fake session's `close()` returns the connection either way, so the
    assertion is about the GENERATOR itself: an abandoned async generator
    still has a live frame, a closed one does not.
    """

    async def _fake_update_user_telegram_chat_id(db, user_id, chat_id, username):
        return None

    monkeypatch.setattr(
        crud, "update_user_telegram_chat_id", _fake_update_user_telegram_chat_id
    )

    factory = _DbFactory()
    handlers = {}

    class _FakeBot:
        def message_handler(self, commands=None):
            def _decorator(fn):
                handlers[commands[0]] = fn
                return fn

            return _decorator

        async def reply_to(self, message, text, **kwargs):
            return None

    redis_client = SimpleNamespace(
        get=AsyncMock(return_value=b"42"),
        delete=AsyncMock(return_value=None),
    )

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.bot = _FakeBot()
    notifier.loop = None
    notifier.setup_handlers(factory, redis_client)

    message = SimpleNamespace(
        text="/start bind-token",
        chat=SimpleNamespace(id=99),
        from_user=SimpleNamespace(username="someone"),
    )

    await handlers["start"](message)

    assert factory.pool.opened == 1, "the Telegram path did not open a session"
    generator = factory.generators[0]
    assert generator.ag_frame is None, (
        "the Telegram path abandoned the session generator: it is still "
        "suspended at its `yield`, inside the `async with AsyncSessionLocal()` "
        "that owns the pooled connection"
    )


# ---------------------------------------------------------------------------
# The structural net (upstream's own regression test)
# ---------------------------------------------------------------------------
def test_no_raw_async_for_over_a_session_factory():
    """Guard: long-lived session reads must go through an owned session.

    `async for db in self.get_db_session()` abandons the generator when the
    body raises, and the abandoned session keeps its transaction OPEN - which
    pins a pooled server connection. On the 2026-10-19 night run that was
    ~120 pinned connections at 660 controllers: the pooler ran out of server
    connections and every query past them timed out.

    The whole `bot_module` package is scanned, not just `controller.py`:
    `rehydrate.py` had the same defect (it is reached once per strategy on
    every bot restart, with the controller's own session factory), and a
    guard that only covers the file it was written for would not have caught
    it.
    """
    package_dir = Path(controller_mod.__file__).resolve().parent
    offenders = []
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFor):
                continue
            it = node.iter
            if not isinstance(it, ast.Call):
                continue
            func = it.func
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            else:
                called = None
            if called in _SESSION_FACTORY_NAMES:
                rel = path.relative_to(package_dir.parent).as_posix()
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "raw `async for ... <session factory>()` pins a connection on error: "
        f"{offenders}"
    )
