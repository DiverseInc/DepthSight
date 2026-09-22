# api/redis_client.py
import redis.asyncio as aioredis
import redis.exceptions as redis_exceptions
from fastapi import HTTPException, status
import logging

from bot_module import config as bot_config

logger = logging.getLogger(__name__)


async def get_redis_client() -> aioredis.Redis:
    """
    Dependency to get a Redis client.
    Attempts to reconnect if the connection is lost.
    """
    try:
        redis_password = bot_config.REDIS_PASSWORD
        if not redis_password:
            logger.warning(
                "SECURITY: Redis is configured WITHOUT authentication (no REDIS_PASSWORD). "
                "Set REDIS_PASSWORD in environment and enable 'requirepass' in redis.conf for production."
            )
        redis_client = aioredis.Redis(
            host=bot_config.REDIS_HOST,
            port=bot_config.REDIS_PORT,
            db=bot_config.REDIS_DB,
            username=bot_config.REDIS_USERNAME,
            password=redis_password,
            decode_responses=True,
        )
        await redis_client.ping()
        return redis_client
    except (
        redis_exceptions.ConnectionError,
        redis_exceptions.BusyLoadingError,
        redis_exceptions.TimeoutError,
        redis_exceptions.AuthenticationError,
    ) as e:
        logger.error(f"Failed to connect to Redis: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not establish connection to Redis service.",
        )


async def get_market_redis_client() -> aioredis.Redis:
    """
    Dependency to get a Redis client for the MARKET data container
    (`depthsight_redis_market`, service `redis-market`).

    This is separate from `get_redis_client` because the bot writes the
    candle-flow health state (market_data:active_streams SET and
    market_data:candle_received:* STRING keys) to this container, while
    running-strategy state lives in the main app Redis.

    Endpoints that need to read either (like /api/v1/market-data/candle-health)
    should declare BOTH dependencies and route queries accordingly.
    """
    try:
        redis_password = bot_config.REDIS_PASSWORD
        redis_client = aioredis.Redis(
            host=bot_config.MARKET_REDIS_HOST,
            port=bot_config.MARKET_REDIS_PORT,
            db=bot_config.MARKET_REDIS_DB,
            username=bot_config.REDIS_USERNAME,
            password=redis_password,
            decode_responses=True,
        )
        await redis_client.ping()
        return redis_client
    except (
        redis_exceptions.ConnectionError,
        redis_exceptions.BusyLoadingError,
        redis_exceptions.TimeoutError,
        redis_exceptions.AuthenticationError,
    ) as e:
        logger.error(f"Failed to connect to market Redis: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not establish connection to market Redis service.",
        )
