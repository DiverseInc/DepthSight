from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio
import json

import pytest

import bot_runner


def test_plan_allows_live_trading_matches_plan_config():
    assert bot_runner._plan_allows_live_trading("standard") is True
    assert bot_runner._plan_allows_live_trading("pro") is True
    assert bot_runner._plan_allows_live_trading("free") is False
    assert bot_runner._plan_allows_live_trading("researcher") is False


def test_api_key_is_sharded_by_api_key_id():
    user_id = 42

    assert user_id % 2 == 0
    assert bot_runner._api_key_belongs_to_shard(1, shard_id=1, num_workers=2) is True
    assert bot_runner._api_key_belongs_to_shard(2, shard_id=1, num_workers=2) is False


@pytest.mark.asyncio
async def test_initialize_user_controllers_uses_api_key_sharding(mocker):
    user = SimpleNamespace(id=42, username="live_user", plan="standard")
    active_keys = [
        SimpleNamespace(id=1, name="key-1"),
        SimpleNamespace(id=2, name="key-2"),
        SimpleNamespace(id=3, name="key-3"),
    ]

    mocker.patch.object(
        bot_runner.crud,
        "get_active_api_keys_for_user",
        AsyncMock(return_value=active_keys),
    )
    initialize_controller = mocker.patch.object(
        bot_runner, "_initialize_controller_for_key", AsyncMock()
    )

    await bot_runner._initialize_user_controllers(
        user,
        db=object(),
        session=object(),
        redis_client=object(),
        telegram_notifier_instance=None,
        shard_id=1,
        num_workers=2,
    )

    initialized_api_key_ids = [
        call.args[1].id for call in initialize_controller.await_args_list
    ]
    assert initialized_api_key_ids == [1, 3]


@pytest.mark.asyncio
async def test_initialize_user_controllers_skips_non_live_plans(mocker):
    user = SimpleNamespace(id=7, username="research_only", plan="researcher")

    get_active_keys = mocker.patch.object(
        bot_runner.crud, "get_active_api_keys_for_user", AsyncMock()
    )
    initialize_controller = mocker.patch.object(
        bot_runner, "_initialize_controller_for_key", AsyncMock()
    )

    await bot_runner._initialize_user_controllers(
        user,
        db=object(),
        session=object(),
        redis_client=object(),
        telegram_notifier_instance=None,
        shard_id=0,
        num_workers=4,
    )

    get_active_keys.assert_not_awaited()
    initialize_controller.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_strategy_runtime_state_publishes_empty_snapshot():
    redis_client = AsyncMock()

    await bot_runner._clear_strategy_runtime_state(
        redis_client, user_id=15, api_key_id=4
    )

    redis_client.set.assert_awaited_once_with(
        "depthsight:state:strategies:15:4",
        "[]",
    )
    redis_client.publish.assert_awaited_once_with(
        "depthsight:events:strategies:15",
        json.dumps({"user_id": 15}),
    )


@pytest.mark.asyncio
async def test_command_listener_releases_its_own_session_after_each_command(mocker):
    """The listener's read session must be released after every command.

    A read opens a transaction that is never closed, pinning one server
    connection per worker for the whole run.

    It must be the listener's OWN session that gets rolled back, and NEVER
    the shard's shared one: the paper executor (bot_module/paper_executor.py
    :81) and the risk manager write through the shared session, flushing now
    and committing later (the wallet credit at paper_executor.py:275 is
    flushed, an await on Redis happens at :280, and only
    paper_executor.py:422 commits it). Rolling the shared session back here
    would silently discard a pending paper-wallet credit or risk-guard
    update - a money-losing regression, not a leak fix.
    """
    listener_session = AsyncMock()
    shared_db = AsyncMock()

    async def _fake_get_db():
        yield listener_session

    mocker.patch.object(bot_runner, "get_db", _fake_get_db)

    calls = {"n": 0}

    async def _get_message(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "type": "message",
                "data": json.dumps({"command": "NOOP", "payload": {}}),
            }
        # The listener loops forever; this ends it.
        raise asyncio.CancelledError()

    pubsub = SimpleNamespace(
        subscribe=AsyncMock(),
        unsubscribe=AsyncMock(),
        get_message=_get_message,
    )
    redis_client = SimpleNamespace(pubsub=lambda: pubsub)

    await bot_runner._run_command_listener(
        shared_db, object(), redis_client, None, 0, 1
    )

    assert calls["n"] == 2, "the listener did not process the command"
    listener_session.rollback.assert_awaited()
    shared_db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_init_does_not_roll_back_the_shared_session(mocker):
    """A failed init must not discard the shared session's pending writes.

    Upstream rolls the bootstrap session back after a failed init, to release
    the connection it pinned. That is safe upstream - upstream gives every
    controller its own session - and it is NOT safe here: in this fork the
    shard's single session is also the write session of the paper executor
    and the risk manager, which flush now and commit later. A rollback from
    the init path would discard their pending paper-wallet credits and
    risk-guard updates. The shared session is therefore never rolled back
    from the init path; releasing it needs the per-controller session
    refactor, which is a separate change.
    """
    mocker.patch.object(bot_runner.security, "decrypt_data", lambda value: "x")
    mocker.patch.object(
        bot_runner,
        "create_exchange_executor",
        MagicMock(side_effect=RuntimeError("exchange unreachable")),
    )

    shared_db = AsyncMock()
    user = SimpleNamespace(id=7, username="pin_user")
    key = SimpleNamespace(
        id=9,
        name="k",
        exchange="binance",
        encrypted_api_key="ek",
        encrypted_api_secret="es",
    )

    ok = await bot_runner._initialize_controller_for_key(
        user, key, shared_db, object(), object(), None
    )

    assert ok is False
    shared_db.rollback.assert_not_awaited()
