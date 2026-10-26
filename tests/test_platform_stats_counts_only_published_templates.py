# tests/test_platform_stats_counts_only_published_templates.py
"""FIX (public counters): the template counter must count the templates the
Discovery Hub actually serves.

`GET /api/v1/stats/platform` counted EVERY row in `strategy_templates`,
including deactivated ones. `GET /api/v1/strategy-templates` serves only the
active ones (`is_active=True`), so the public "Templates" number was one
higher than the number of templates a visitor can actually see.

Same class of defect as the demo-account counter: a public counter that
overstates the product.
"""

import pytest

from api import models


def _template(slug: str, name: str, is_active: bool) -> models.StrategyTemplate:
    return models.StrategyTemplate(
        slug=slug,
        name=name,
        archetype="rsi_breakout",
        config_data={"blocks": []},
        risk_profile={"max_position_size": 1.0},
        is_active=is_active,
    )


@pytest.mark.asyncio
async def test_a_deactivated_template_is_not_counted(db_session, test_client):
    before = (
        await test_client.get("/api/v1/stats/platform")
    ).json()["data"]["strategyTemplates"]

    db_session.add(_template("stats-active-tpl", "Active template", True))
    db_session.add(_template("stats-inactive-tpl", "Deactivated template", False))
    await db_session.commit()

    after = (
        await test_client.get("/api/v1/stats/platform")
    ).json()["data"]["strategyTemplates"]

    # Only the ACTIVE template is a template a visitor can see. The
    # deactivated one must not be advertised.
    assert after == before + 1, (
        "The public template counter counted a deactivated template. "
        "It advertised more templates than the Hub actually serves."
    )


@pytest.mark.asyncio
async def test_the_counter_matches_the_templates_the_hub_serves(
    db_session, test_client
):
    """The counter and the list endpoint must agree.

    Otherwise the public page advertises a different number of templates from
    the one the visitor finds when they click through.
    """
    db_session.add(_template("stats-active-tpl-2", "Active template 2", True))
    db_session.add(_template("stats-inactive-tpl-2", "Deactivated template 2", False))
    await db_session.commit()

    stats = (await test_client.get("/api/v1/stats/platform")).json()["data"]
    listed = (await test_client.get("/api/v1/strategy-templates")).json()["data"]

    assert stats["strategyTemplates"] == len(listed), (
        "The public template counter and the list of templates served to a "
        "visitor disagree."
    )
    assert stats["verifiedTemplates"] == stats["strategyTemplates"]
