"""The regime-change stop must not be ARMED on a deployment that cannot feed it.

WHY
---
`breakeven_on_regime_change` is implemented in `check_on_screener_update`
(bot_module/strategy.py:3070-3079). Its ONLY production caller is
`_dynamic_symbol_selection_loop` (bot_module/controller.py:5057), and that
loop consumes the controller's screener-update queue. The ONLY producer of
that queue is the `active_pairs_update` message on the main_app WebSocket
(bot_module/data_consumer.py:3378).

So the regime-change stop is delivered by exactly one thing: the screener
WebSocket feed. No feed, no stop -- but the strategy still starts, still
reports "running", and the user believes they are protected.

The DYNAMIC guard next door (api/routes/strategies.py:566) already refuses
DYNAMIC for the same reason. This is the identical reasoning applied to the
identical failure mode.

THE PREDICATE (derived, not guessed)
------------------------------------
The socket that carries the regime data is created in exactly one place --
bot_module/data_consumer.py:453-478 -- and it needs ALL THREE of:

  1. SYMBOL_SOURCE_MODE == "MAIN_APP"    (data_consumer.py:453, 456)
  2. MAIN_APP_WS_ENABLED is true          (data_consumer.py:463)
  3. MAIN_APP_WS_URL is a ws:// / wss:// URL
                                           (data_consumer.py:469-472)

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
