"""
StrategyTemplate routes — curated verified strategy templates surfaced on
the Discovery Hub's "Verified Templates" tab. Replaces the previous
CORS-blocked external call to app.depthsight.pro on self-hosted deployments.

These endpoints are PUBLIC (no auth required) so the Hub can be browsed by
unauthenticated visitors. The "Use Template" import flow (separate POST
endpoint, when added) will require auth.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud, schemas
from ..database import get_db

logger = logging.getLogger(__name__)

# Public router — no auth dependency, so the Hub page works for unauthenticated
# visitors and avoids a 401 when the frontend calls without an Authorization header.
templates_router = APIRouter(
    tags=["Strategy Templates"],
)


@templates_router.get(
    "/strategy-templates",
    response_model=schemas.ApiResponseData[list[schemas.StrategyTemplate]],
    summary="List all verified strategy templates",
)
async def list_strategy_templates(
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the 6 built-in strategy archetypes (RSI Breakout, EMA 50/200,
    Bollinger Mean Reversion, Grid DCA, Order Book Imbalance Scalper,
    ML-Confirmed Trend) plus any admin-added templates.

    The list is filtered by is_active=True. Public — no auth required.
    tier_required is a hint to the UI to gate the "Use this template" button.
    """
    templates = await crud.list_strategy_templates(db, active_only=True)
    return {
        "data": [
            schemas.StrategyTemplate.model_validate(t) for t in templates
        ]
    }


@templates_router.get(
    "/strategy-templates/{slug}",
    response_model=schemas.ApiResponseData[schemas.StrategyTemplate],
    summary="Get a single strategy template by slug",
)
async def get_strategy_template(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    template = await crud.get_strategy_template_by_slug(db, slug)
    if not template or not template.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy template '{slug}' not found.",
        )
    return {"data": schemas.StrategyTemplate.model_validate(template)}
