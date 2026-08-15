"""Local community content endpoints — News, Trading Ideas, Discussions.
Replaces the central hub calls on self-hosted deployments.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud, models, schemas
from ..auth import get_current_user
from ..database import get_db

logger = logging.getLogger(__name__)

community_router = APIRouter(
    prefix="/api/v1/community",
    tags=["Community Content"],
    dependencies=[Depends(get_current_user)],
)


@community_router.get("/news")
async def list_news(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """List platform news / announcements / changelog posts."""
    items = await crud.list_hub_news(db, limit=limit)
    return {
        "data": [
            {
                "id": n.id,
                "title": n.title,
                "text": n.text,
                "date": n.date,
                "likesCount": n.likes_count,
                "isPinned": n.is_pinned,
                "createdAt": n.created_at.isoformat() if n.created_at else None,
            }
            for n in items
        ]
    }


@community_router.get("/topics")
async def list_topics(
    type: Optional[str] = Query(None, description="'strategy' or 'discussion'"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """List community topics — trading ideas (type=strategy) or discussions."""
    if type and type not in ("strategy", "discussion"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="type must be 'strategy' or 'discussion'")
    items = await crud.list_hub_topics(db, topic_type=type, limit=limit)
    return {
        "data": [
            {
                "id": t.id,
                "topicType": t.topic_type,
                "title": t.title,
                "description": t.description,
                "authorName": t.author_name,
                "symbol": t.symbol,
                "periodStart": t.period_start,
                "periodEnd": t.period_end,
                "kpis": t.kpis,
                "createdAt": t.created_at.isoformat() if t.created_at else None,
            }
            for t in items
        ]
    }
