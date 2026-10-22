"""The regime-change stop must not be ARMED on a deployment that cannot feed it.

WHY
---
`breakeven_on_regime_change` is implemented in `check_on_screener_update`
(bot_module/strategy.py:3070-3079). Its ONLY production caller is
`_dynamic_symbol_selection_loop` (bot_module/controller.py:5213), and that
loop consumes the controller's screener-update queue. The ONLY producer of
that queue is the `active_pairs_update` message on the main_app WebSocket
(bot_module/data_consumer.py:3428).

So the regime-change stop is delivered by exactly one thing: the screener
WebSocket feed. No feed, no stop -- but the strategy still starts, still
reports "running", and the user believes they are protected.

The DYNAMIC guard next door (api/routes/strategies.py:583) already refuses
DYNAMIC for the same reason. This is the identical reasoning applied to the
identical failure mode.

THE PREDICATE (derived, not guessed)
------------------------------------
The socket that carries the regime data is created in exactly one place --
bot_module/data_consumer.py:453-526 -- and it needs ALL THREE of:

  1. SYMBOL_SOURCE_MODE == "MAIN_APP"    (data_consumer.py:453, 456)
  2. MAIN_APP_WS_ENABLED is true          (data_consumer.py:463)
  3. MAIN_APP_WS_URL is a ws:// / wss:// URL
                                           (data_consumer.py:519-522)

The guard below fires only when the option is armed AND one of those three
fails. It is a CONFIGURATION test: MAIN_APP_WS_URL has a code default
(bot_module/config.py:507) and nothing here can prove the host is answering.
The honest claim is "this deployment cannot deliver the regime data", not
"the feed is down".

NO FALSE POSITIVES
------------------
A deployment that satisfies all three keeps the protection -- see
`test_a_working_feed_still_allows_the_stop_to_be_armed`, which asserts 202
and not merely "not 503". Refusing a stop that would have worked would be a
regression, so that case is pinned explicitly.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from api import crud, models, schemas
from bot_module import config as bot_config


@pytest.fixture
def feed_configured(monkeypatch):
    """The deployment that CAN deliver the regime data."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "MAIN_APP")
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", True)
    monkeypatch.setattr(
        bot_config, "MAIN_APP_WS_URL", "wss://screener.example.test/ws/bot/"
    )


async def _make_config(
    db_session, *, user_id: int, name: str, mode: str, config_data: dict
) -> models.StrategyConfig:
    """Create a real strategy config so the endpoint runs its real code path.

    `user_id` must match the authenticated client, or the endpoint's ownership
    lookup 404s before it ever reaches the guard.
    """
    created = await crud.create_strategy_config(
        db_session,
        user_id=user_id,
        config_create=schemas.StrategyConfigCreate(
            name=name,
            config_data=config_data,
            symbol_selection_mode=mode,
        ),
    )
    await db_session.commit()
    await db_session.refresh(created)
    return created


async def _launch(client: AsyncClient, config_id: str, **extra):
    body = {"config_id": config_id}
    body.update(extra)
    return await client.post("/api/v1/strategies", json=body)


# --------------------------------------------------------------- the refusals


async def test_arming_the_stop_without_a_screener_feed_is_refused_with_503(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    monkeypatch,
):
    """THE CASE: STATIC_LIST mode cannot deliver regime data, so refuse.

    Before the fix the strategy started with 202 and its regime-change stop
    was inert from the first second.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-without-a-feed",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "A strategy that arms breakeven_on_regime_change must be refused with "
        "503 when the deployment has no screener feed to deliver the regime "
        f"data. Got {response.status_code}: {response.text}"
    )
    # Pin the exact body shape the app's HTTPException handler produces
    # (api/depthsight_api.py:2196-2200), so this new response cannot quietly
    # drift away from the shape the neighbouring DYNAMIC 503 already uses.
    body = response.json()
    assert body["detail"] is None, (
        f"the 503 must use the app's HTTPException body shape. Got: {body!r}"
    )
    assert "breakeven_on_regime_change" in body["error"], (
        f"the 503 must name the option it refused. Got: {body!r}"
    )


async def test_arming_the_stop_with_the_ws_loop_disabled_is_refused_with_503(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_configured,
    monkeypatch,
):
    """MAIN_APP_WS_ENABLED=false means the socket is never created."""
    monkeypatch.setattr(bot_config, "MAIN_APP_WS_ENABLED", False)

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-ws-disabled",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "MAIN_APP_WS_ENABLED=false removes the only producer of the regime "
        f"data, so arming the stop must be refused. Got {response.status_code}: "
        f"{response.text}"
    )


async def test_arming_the_stop_with_an_unusable_ws_url_is_refused_with_503(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_configured,
    monkeypatch,
):
    """A URL that is not ws:// or wss:// cannot carry the feed."""
    monkeypatch.setattr(
        bot_config, "MAIN_APP_WS_URL", "https://screener.example.test/ws/bot/"
    )

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-bad-url",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 503, (
        "MAIN_APP_WS_URL that is not a ws:// / wss:// URL cannot deliver the "
        f"regime data. Got {response.status_code}: {response.text}"
    )


async def test_arming_the_stop_through_the_launch_override_is_refused_with_503(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    monkeypatch,
):
    """The launch-time override channel must be covered too.

    The frontend arms the option through `params`
    (frontend/src/pages/Strategies.tsx:660), so a saved config that does not
    arm it can still be armed at launch time.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-via-override",
        mode="FIXED",
        config_data={"strategy_name": "Fixed", "params": {}},
    )

    response = await _launch(
        authenticated_client,
        cfg.id,
        params={"breakeven_on_regime_change": True},
    )

    assert response.status_code == 503, (
        "Arming the stop through the launch-time params override must be "
        f"refused the same way. Got {response.status_code}: {response.text}"
    )


# ------------------------------------------------------------ no false alarms


async def test_a_working_feed_still_allows_the_stop_to_be_armed(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    feed_configured,
):
    """NO FALSE POSITIVE: a deployment that can deliver the data keeps it.

    Asserts 202 exactly, not merely "!= 503": an "isn't refused" assertion
    would also pass on a 404, 403 or 500, hiding an unrelated break behind
    this test. Refusing a stop that WOULD have worked is a regression, so the
    working case is pinned explicitly.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-with-a-working-feed",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        "A deployment whose screener feed is configured CAN deliver the "
        "regime data, so the stop must be allowed to arm. Got "
        f"{response.status_code}: {response.text}"
    )


async def test_a_strategy_that_does_not_arm_the_stop_is_never_refused(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    monkeypatch,
):
    """The guard must only fire for strategies that arm the option."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="no-regime-change-no-feed",
        mode="FIXED",
        config_data={"strategy_name": "Fixed", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        "A strategy that does not arm the regime-change stop needs no "
        f"screener feed. Got {response.status_code}: {response.text}"
    )


async def test_the_option_explicitly_set_to_false_is_not_refused(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    monkeypatch,
):
    """`False` is not `armed`. It must not be refused."""
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-explicitly-off",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": False,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        f"Got {response.status_code}: {response.text}"
    )


# ------------------------------------------------- the approval record (write side)


def _published_start_payloads(redis_mock) -> list:
    """Every START_STRATEGY payload the launch published, decoded."""
    payloads = []
    for _channel, message in getattr(redis_mock, "publish_calls", []):
        try:
            decoded = json.loads(message)
        except (TypeError, ValueError):
            continue
        if decoded.get("command") == "START_STRATEGY":
            payloads.append(decoded["payload"])
    return payloads


def _cached_payload(redis_mock, user_id: int, config_id: str) -> dict:
    """The payload the launch cached for the bot to replay after a restart."""
    raw = redis_mock._data[f"running_strategy_payload:{user_id}:{config_id}"]
    return json.loads(raw)


async def test_the_api_records_the_approval_state_at_launch(
    authenticated_client: AsyncClient,
    mock_redis_client,
    db_session,
    pro_user,
    feed_configured,
):
    """THE WRITE SIDE: an armed launch must record that it was approved.

    `PUT /config/{config_id}` writes `config_data` WITHOUT applying the
    regime-change guard, so after an approval the current config can carry an
    arming that was never approved. The bot's rehydrate reads `config_data`
    back from Postgres, so if the approval state lived only in `config_data`
    it would be indistinguishable from a later, never-approved arming.

    Nothing else records this. The API is the only place that knows what was
    approved, so if it does not write the record the bot has nothing to read
    and every replayed armed strategy is treated as never-approved.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-approval-record",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        f"Got {response.status_code}: {response.text}"
    )

    published = _published_start_payloads(mock_redis_client)
    assert len(published) == 1, (
        f"the launch must publish exactly one START_STRATEGY. Got {published!r}"
    )
    payload = published[0]
    assert payload.get("approved_breakeven_on_regime_change") is True, (
        "the API must record that this launch was approved WITH the stop "
        "armed, or the rehydrate will refuse a strategy the guard allowed"
    )

    # The record must survive into the CACHED payload too. The bot replays
    # from Redis on restart, not from this publish, so a record that is not
    # cached is a record the bot never sees.
    cached = _cached_payload(mock_redis_client, pro_user.id, cfg.id)
    assert cached.get("approved_breakeven_on_regime_change") is True, (
        "the cached replay payload must carry the approval record, because the "
        f"rehydrate reads that key and not the published command. Got {cached!r}"
    )


async def test_the_api_records_a_launch_that_did_not_arm_the_stop_as_not_approved(
    authenticated_client: AsyncClient,
    mock_redis_client,
    db_session,
    pro_user,
    monkeypatch,
):
    """A strategy that never armed the option must record `False`, not omit it.

    Absent is the BACKWARD-COMPATIBILITY case, which deliberately falls back to
    the payload's own arming and logs a warning. A launch on this code path
    always knows its answer, so omitting the key here would push every new
    launch onto the fallback path and drown the real, genuinely-legacy payloads
    in noise.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SOURCE_MODE", "STATIC_LIST")

    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-no-approval-record",
        mode="FIXED",
        config_data={"strategy_name": "Fixed", "params": {}},
    )

    response = await _launch(authenticated_client, cfg.id)

    assert response.status_code == 202, (
        f"Got {response.status_code}: {response.text}"
    )

    payload = _published_start_payloads(mock_redis_client)[0]
    assert payload.get("approved_breakeven_on_regime_change") is False, (
        "a launch that did not arm the stop must record False explicitly -- "
        "an ABSENT key means 'predates the record' and takes the fallback path"
    )


async def test_the_approval_record_rides_as_a_sibling_of_config_data(
    authenticated_client: AsyncClient,
    mock_redis_client,
    db_session,
    pro_user,
    feed_configured,
):
    """PIN THE DESIGN: the record is a sibling, never a key INSIDE config_data.

    `bot_module/rehydrate.refresh_payload_config_from_db` does
    `updated = dict(payload)` and then `updated["config_data"] = fresh`, so it
    replaces the WHOLE config_data and leaves every sibling alone. A record
    written inside config_data is replaced with the Postgres copy on the first
    restart and the approval signal is destroyed -- which is exactly the defect
    this PR closes.

    Asserting the record is absent from config_data is what keeps a future
    refactor from moving it back inside and silently reintroducing the hole
    while every other test still passes.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="regime-change-record-placement",
        mode="FIXED",
        config_data={
            "strategy_name": "Fixed",
            "params": {},
            "breakeven_on_regime_change": True,
        },
    )

    response = await _launch(authenticated_client, cfg.id)
    assert response.status_code == 202, f"Got {response.status_code}: {response.text}"

    payload = _published_start_payloads(mock_redis_client)[0]
    assert "approved_breakeven_on_regime_change" not in payload["config_data"], (
        "the approval record must NOT live inside config_data: the rehydrate "
        "replaces that whole key with the Postgres copy, so a record placed "
        "there is destroyed on the first restart"
    )
    assert "approved_breakeven_on_regime_change" in payload, (
        "the approval record must be a sibling of config_data so it survives "
        "the refresh and the write-back"
    )
