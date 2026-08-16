"""Public platform stats endpoint — powers the dashboard activity strip.

Returns aggregate counts across the platform. PUBLIC (no auth) so the strip
can be visible to non-logged-in visitors on the marketing page too.

Numbers exposed:
- totalUsers: count of users in the system
- strategyTemplates: count of curated templates on the Hub
- communityNews: count of platform announcements
- communityStrategyTopics: count of community trading ideas
- communityDiscussionTopics: count of community discussion threads
- totalBacktests: count of backtest runs across all users
- verifiedTemplates: alias of strategyTemplates (kept for backward compat with the frontend)
"""
import logging
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Platform Stats"])


@router.get("/stats/platform")
async def platform_stats(db: AsyncSession = Depends(get_db)):
    """Public platform-wide stats. Used by the dashboard activity strip."""
    try:
        users = (
            await db.execute(select(func.count(models.User.id)))
        ).scalar() or 0
        templates = (
            await db.execute(select(func.count(models.StrategyTemplate.id)))
        ).scalar() or 0
        news = (
            await db.execute(select(func.count(models.HubNewsItem.id)))
        ).scalar() or 0
        strategy_topics = (
            await db.execute(
                select(func.count(models.HubTopic.id)).where(
                    models.HubTopic.topic_type == "strategy"
                )
            )
        ).scalar() or 0
        discussion_topics = (
            await db.execute(
                select(func.count(models.HubTopic.id)).where(
                    models.HubTopic.topic_type == "discussion"
                )
            )
        ).scalar() or 0
        backtests = (
            await db.execute(select(func.count(models.BacktestRun.id)))
        ).scalar() or 0
    except Exception as e:
        logger.exception(f"Failed to compute platform stats: {e}")
        # Return zeros rather than 500ing — the strip is non-critical UI
        return {
            "data": {
                "totalUsers": 0,
                "strategyTemplates": 0,
                "communityNews": 0,
                "communityStrategyTopics": 0,
                "communityDiscussionTopics": 0,
                "totalBacktests": 0,
                "verifiedTemplates": 0,
            }
        }

    return {
        "data": {
            "totalUsers": users,
            "strategyTemplates": templates,
            "communityNews": news,
            "communityStrategyTopics": strategy_topics,
            "communityDiscussionTopics": discussion_topics,
            "totalBacktests": backtests,
            "verifiedTemplates": templates,  # alias
            "communityPosts": strategy_topics + discussion_topics,  # aggregate
        }
    }
