"""
Coverage for the DYNAMIC symbol-selection guard (api/routes/strategies.py:566).

This guard had NO test coverage at all, which is how five unrelated tests came
to fail with a 503 where they expected 202 or 403 without anyone noticing the
guard itself was untested. The conftest now enables DYNAMIC selection for tests
so those five can reach the behaviour they were written to check; these tests
exist to prove the production guard still behaves, so the conftest change can
never quietly disable it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from api import crud, models, schemas
from api.database import get_db
from bot_module import config as bot_config


@pytest.fixture
def dynamic_selection_disabled(monkeypatch):
    """Turn the guard back on for the duration of one test.

    The autouse conftest fixture sets this True; the guard is only reachable
    with it False.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", False)


async def _make_config(
    db_session, *, user_id: int, name: str, mode: str, config_data: dict
) -> models.StrategyConfig:
    """Create a real strategy config so the endpoint runs its real code path.

    `user_id` must match the authenticated client, or the endpoint's ownership
    lookup 404s before it ever reaches the symbol-selection guard.
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


async def test_dynamic_strategy_is_refused_with_503_when_disabled(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    dynamic_selection_disabled,
):
    """The point of the guard: refuse loudly rather than start and never trade."""
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name="dynamic-should-be-refused",
        mode="DYNAMIC",
        config_data={"strategy_name": "Dyn", "params": {}},
    )

    response = await authenticated_client.post(
        "/api/v1/strategies", json={"config_id": cfg.id}
    )

    assert response.status_code == 503, (
        "A DYNAMIC strategy must be refused with 503 when no screener is "
        "reachable. If this returns 202, the strategy would start, report "
        "'running', and never trade -- the exact failure this guard exists "
        "to prevent."
    )


@pytest.mark.parametrize("mode", ["STATIC", "FIXED"])
async def test_explicit_symbol_modes_are_unaffected_by_the_guard(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    dynamic_selection_disabled,
    mode: str,
):
    """STATIC and FIXED are what the onboarding wizard creates and must keep
    working while the guard is on.

    Asserts 202 exactly, not merely "!= 503": an "isn't refused" assertion would
    also pass on a 404, 403 or 500, which would hide an unrelated break behind
    this test. The point is that these modes START, not merely that the guard
    stays quiet.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name=f"{mode.lower()}-strategy",
        mode=mode,
        config_data={"strategy_name": mode, "params": {}},
    )

    response = await authenticated_client.post(
        "/api/v1/strategies", json={"config_id": cfg.id}
    )

    assert response.status_code == 202, (
        f"{mode} must start normally with the dynamic-selection guard enabled. "
        f"Got {response.status_code}: {response.text}"
    )


# --- every DYNAMIC spelling, not only the literal "DYNAMIC" -----------------
#
# The API takes `symbol_selection_mode` as a FREE STRING, and the runtime's
# own dynamic modes are "DYNAMIC_NATR" and "DYNAMIC_ORACLE"
# (bot_module/symbol_selection.py:82). The guard matched only the exact
# string "DYNAMIC", so the other two walked past it -- and the bot's
# symbol-pool resolver returns NO symbols for a mode it does not recognise
# (bot_module/controller.py:7271), so such a strategy started, reported
# "running", and never traded: the exact failure the guard exists to prevent.

DYNAMIC_MODES = ["DYNAMIC", "DYNAMIC_NATR", "DYNAMIC_ORACLE"]


@pytest.fixture
def dynamic_selection_enabled(monkeypatch):
    """The operator escape hatch: an operator running their own screener.

    The autouse conftest fixture already leaves this True; setting it
    explicitly here means the escape-hatch test proves the flag's own
    behaviour instead of relying on the conftest's default.
    """
    monkeypatch.setattr(bot_config, "SYMBOL_SELECTION_ALLOW_DYNAMIC", True)


@pytest.mark.parametrize("mode", DYNAMIC_MODES)
async def test_every_dynamic_mode_is_refused_with_503_when_disabled(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    dynamic_selection_disabled,
    mode: str,
):
    """Every dynamic spelling must be refused, not only "DYNAMIC".

    Names the mode the way the RUNTIME does. "DYNAMIC_NATR" and
    "DYNAMIC_ORACLE" are not the string "DYNAMIC", so a guard that compares
    for equality lets them through -- and the strategy then starts with an
    EMPTY symbol list, reports "running", and never trades.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name=f"{mode.lower()}-strategy",
        mode=mode,
        config_data={"strategy_name": mode, "params": {}},
    )

    response = await authenticated_client.post(
        "/api/v1/strategies", json={"config_id": cfg.id}
    )

    assert response.status_code == 503, (
        f"{mode} must be refused with 503 when no screener is reachable. "
        f"Got {response.status_code}: {response.text}. If this returns 202, "
        f"the strategy would start, report 'running', and never trade -- the "
        f"exact failure this guard exists to prevent."
    )

    detail = response.json().get("error") or response.json().get("detail") or ""
    assert isinstance(detail, str) and detail.strip(), (
        "The refusal must carry a plain explanation, not an empty body. "
        f"Got: {response.json()!r}"
    )
    assert "not available" in detail.lower(), (
        f"The 503 must explain itself in plain words. Got: {detail!r}"
    )


@pytest.mark.parametrize("mode", DYNAMIC_MODES)
async def test_a_request_override_naming_a_dynamic_mode_is_refused_with_503(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    dynamic_selection_disabled,
    mode: str,
):
    """The launch request's OWN mode override must be guarded too.

    The saved config is STATIC here, so the only thing asking for a dynamic
    pool is the request body -- which is exactly what a direct API caller
    can send.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name=f"override-{mode.lower()}",
        mode="STATIC",
        config_data={"strategy_name": mode, "params": {}},
    )

    response = await authenticated_client.post(
        "/api/v1/strategies",
        json={"config_id": cfg.id, "symbol_selection_mode": mode},
    )

    assert response.status_code == 503, (
        f"A request that names {mode} must be refused with 503 when no "
        f"screener is reachable. Got {response.status_code}: {response.text}. "
        f"If this returns 202, the strategy would start with an EMPTY symbol "
        f"list, report 'running', and never trade."
    )


@pytest.mark.parametrize("mode", DYNAMIC_MODES)
async def test_the_operator_escape_hatch_allows_every_dynamic_mode(
    authenticated_client: AsyncClient,
    db_session,
    pro_user,
    dynamic_selection_enabled,
    mode: str,
):
    """With SYMBOL_SELECTION_ALLOW_DYNAMIC=true the guard must not over-block.

    This is the escape hatch the 503 message points operators at. If the
    guard refused these anyway, the operator would have no way to run a
    dynamic strategy at all.
    """
    cfg = await _make_config(
        db_session,
        user_id=pro_user.id,
        name=f"allowed-{mode.lower()}",
        mode=mode,
        config_data={"strategy_name": mode, "params": {}},
    )

    response = await authenticated_client.post(
        "/api/v1/strategies", json={"config_id": cfg.id}
    )

    assert response.status_code == 202, (
        f"{mode} must start when the operator has enabled dynamic selection. "
        f"Got {response.status_code}: {response.text}"
    )
