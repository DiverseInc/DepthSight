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