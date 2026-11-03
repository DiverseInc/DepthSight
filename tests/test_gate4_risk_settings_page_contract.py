"""Gate 4, Part D: the Risk Settings page must be able to SAVE.

WHAT THIS FILE PROVES
======================
1. The EXACT payload ``frontend/src/pages/Settings.tsx`` builds for the Risk
   Management section saves for a REAL account -- one created through the real
   ``crud.create_user`` path, so its stored ``risk_management`` block is the
   block a real client holds.
2. The value the server SERVED on GET is a value the server ACCEPTS on PUT.
   That is the invariant the page depends on: a client sends back what it was
   shown, so a served value the write guard refuses bricks the page.
3. A limit of 0 is REFUSED, and NOTHING is written. A stored 0 is not a
   tighter limit, it is a brick: the RiskManager compares the live value with
   ``>=`` (``bot_module/risk_manager.py`` ``is_symbol_trading_allowed``), so a
   0 limit is true before the first trade is ever placed. The page used to
   turn an EMPTY field into 0 (``parseFloat(x) || 0``), which silently wrote a
   limit that stops all trading.
4. ``riskPerTradePercent = 0`` is still LEGAL: it means "risk off". The
   RiskManager reads it with ``or 0.0``
   (``bot_module/risk_manager.py:181-183``), and the strategy contract says
   the same (``bot_module/strategy_registry.py``). Refusing it would take a
   setting away from the client.

EVERY PAYLOAD HERE IS COMPLETE. On purpose. MEASURED 2026-10-31: the first
version of this file sent a ONE-FIELD body (``{"riskManagement": {"maxDrawdown": 0}}``)
and every "a zero is refused" case PASSED -- for the wrong reason. Pydantic
rejects the body because ``maxDrawdown``, ``maxConcurrentTrades`` and
``stopLossEnabled`` are REQUIRED, so the 422 said "field required" and never
reached the limit check. A test that passes without exercising the fix proves
nothing. Every body below is the page's whole risk block with one field
changed, so the ONLY reason a request can be refused is the field under test.

WHY THIS FILE IS NOT ABOUT "THE PAGE CANNOT SAVE"
==================================================
An earlier probe of this round built a ``risk_management`` block BY HAND with
five keys and no ``maxStopDistancePct``, and the guard refused the page's 10.0
with a 422. MEASURED afterwards: ``maxStopDistancePct`` has been in the
schema since the first commit (2026-06-08) and EVERY creation path
(``api/crud.py`` ``create_user``, ``create_oauth_user``,
``create_default_config_for_existing_user``) stores it, because
``model_dump()`` materialises the schema default. So no real account holds a
block without it, and that 422 was an artefact of a synthetic block, not a
product defect. MEASURED on the real account: the page's own payload saves.

The defect that IS real is #3: the 0. That one is reachable from the page by
clearing a field, and no test in the suite covered it.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from sqlalchemy import select

from api import models

CONFIG_URL = "/api/v1/config"

# The five account-level limits the write guard bounds
# (``bot_module/strategy_registry.py`` ``RISK_FIELD_TO_RM_KEY``).
THE_GUARDED_LIMITS = (
    "maxDrawdown",
    "dailyMaxLossPercent",
    "maxConsecutiveLosses",
    "maxStopDistancePct",
    "riskPerTradePercent",
)

# Plus the one the page also sends that the guard does not bound, but the
# RiskManager reads: a 0 here stops every trade just as surely.
#
# ``riskPerTradePercent`` is DELIBERATELY NOT in this list: a 0 there means
# "risk off" and is legal (see ``test_risk_off_is_still_allowed``).
THE_LIMITS_WHERE_ZERO_IS_A_BRICK = (
    "maxDrawdown",
    "dailyMaxLossPercent",
    "maxConsecutiveLosses",
    "maxStopDistancePct",
    "maxConcurrentTrades",
)


async def _stored_block(db_session, user_id: int, section: str = "risk_management"):
    """The block the DATABASE holds -- not the one the API serves."""
    db_session.expire_all()
    result = await db_session.execute(
        select(models.AppConfig).where(models.AppConfig.user_id == user_id)
    )
    config = result.scalars().first()
    assert config is not None, "the account has no AppConfig row"
    return dict(getattr(config, section) or {})


def _the_payload_the_page_sends(served: Dict[str, Any], **overrides: Any):
    """The EXACT payload ``frontend/src/pages/Settings.tsx`` builds.

    Built from the values the server SERVED, because that is what the page
    does: it loads the config into its inputs and posts them back. Every field
    the page sends for the Risk Management section is here, in the page's
    order, so this payload is the one the browser puts on the wire. The
    ``overrides`` change ONE field and leave every other field at the value the
    server served, so a refusal can only be about the field under test.
    """
    rm = served["riskManagement"]
    block: Dict[str, Any] = {
        "maxDrawdown": rm["maxDrawdown"],
        "maxConsecutiveLosses": rm["maxConsecutiveLosses"],
        "maxConcurrentTrades": rm["maxConcurrentTrades"],
        "stopLossEnabled": rm["stopLossEnabled"],
        "defaultStopLossPercent": rm["defaultStopLossPercent"],
        "maxStopDistancePct": rm["maxStopDistancePct"],
        "dailyMaxLossPercent": rm["dailyMaxLossPercent"],
        "strategySymbolAdjustmentEnabled": rm["strategySymbolAdjustmentEnabled"],
        "strategySymbolWindowSize": rm["strategySymbolWindowSize"],
        "strategySymbolMinTradesForAssessment": rm[
            "strategySymbolMinTradesForAssessment"
        ],
        "strategySymbolPnlThresholdPct": rm["strategySymbolPnlThresholdPct"],
        "strategySymbolWinRateThresholdPct": rm["strategySymbolWinRateThresholdPct"],
        "strategySymbolMaxConsecutiveLosses": rm["strategySymbolMaxConsecutiveLosses"],
        "strategySymbolRecoveryConsecutiveWins": rm[
            "strategySymbolRecoveryConsecutiveWins"
        ],
        "strategySymbolRecoveryPnlThresholdPct": rm[
            "strategySymbolRecoveryPnlThresholdPct"
        ],
        "strategySymbolCooldownAfterPenaltySeconds": rm[
            "strategySymbolCooldownAfterPenaltySeconds"
        ],
    }
    block.update(overrides)
    return {"riskManagement": block}


async def _serve_config(authenticated_client) -> Dict[str, Any]:
    response = await authenticated_client.get(CONFIG_URL)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- item 1: the page's own payload saves -----------------------------------


@pytest.mark.asyncio
async def test_the_risk_settings_page_payload_saves(
    authenticated_client, db_session, pro_user
):
    """The payload the page builds is accepted, and the limit is stored."""
    served = await _serve_config(authenticated_client)
    payload = _the_payload_the_page_sends(served)

    response = await authenticated_client.put(CONFIG_URL, json=payload)

    assert response.status_code == 200, response.text
    stored = await _stored_block(db_session, pro_user.id)
    assert stored["maxDrawdown"] == payload["riskManagement"]["maxDrawdown"]
    assert stored["maxStopDistancePct"] == payload["riskManagement"]["maxStopDistancePct"]


@pytest.mark.asyncio
async def test_every_served_limit_is_a_limit_the_server_accepts(
    authenticated_client, db_session, pro_user
):
    """A served value the write guard refuses would brick the page.

    The client posts back what it was shown. So every limit the server serves
    must survive a write of itself -- otherwise the page cannot save.
    """
    served = await _serve_config(authenticated_client)
    rm = served["riskManagement"]

    for limit in THE_GUARDED_LIMITS:
        assert limit in rm, f"the server does not serve {limit}"
        response = await authenticated_client.put(
            CONFIG_URL,
            json=_the_payload_the_page_sends(served, **{limit: rm[limit]}),
        )
        assert response.status_code == 200, (
            f"the server serves {limit}={rm[limit]} and then refuses it: "
            f"{response.text}"
        )


# --- item 2: a limit of 0 is a brick, not a tighter limit --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit",
    list(THE_LIMITS_WHERE_ZERO_IS_A_BRICK),
)
async def test_a_zero_limit_is_refused_and_nothing_is_written(
    authenticated_client, db_session, pro_user, limit
):
    """A 0 limit stops every trade, so it is refused and NOT stored.

    The page used to send 0 for an EMPTY field (``parseFloat(x) || 0``). A
    stored 0 is compared with ``>=`` by the RiskManager, so it is true before
    the first trade: the account stops trading and nothing says why.
    """
    served = await _serve_config(authenticated_client)
    before = await _stored_block(db_session, pro_user.id)

    response = await authenticated_client.put(
        CONFIG_URL, json=_the_payload_the_page_sends(served, **{limit: 0})
    )

    assert response.status_code == 422, response.text
    after = await _stored_block(db_session, pro_user.id)
    assert after[limit] == before[limit], "a refused limit was still written"


@pytest.mark.asyncio
async def test_risk_off_is_still_allowed(authenticated_client, db_session, pro_user):
    """``riskPerTradePercent = 0`` means "risk off" and stays legal.

    ``bot_module/risk_manager.py:181-183`` reads it with ``or 0.0``, so 0 is a
    REAL setting here and not an unset one. Refusing it would take a setting
    away from the client.
    """
    served = await _serve_config(authenticated_client)

    response = await authenticated_client.put(
        CONFIG_URL,
        json=_the_payload_the_page_sends(served, riskPerTradePercent=0),
    )

    assert response.status_code == 200, response.text
    stored = await _stored_block(db_session, pro_user.id)
    assert stored["riskPerTradePercent"] == 0
