from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from app import settings

logger = logging.getLogger(__name__)

_shared_redis: redis.Redis | None = None


def create_redis_client() -> redis.Redis:

    if settings.REDIS_HOST is None:
        raise RuntimeError("REDIS_HOST environment variable is required")
    if settings.REDIS_PORT is None:
        raise RuntimeError("REDIS_PORT environment variable is required")
    if settings.REDIS_DB is None:
        raise RuntimeError("REDIS_DB environment variable is required")
    if not settings.AUCTION_CACHE_REDIS_KEY:
        raise RuntimeError("AUCTION_CACHE_REDIS_KEY environment variable is required")
    if not settings.AUCTION_CACHE_META_KEY:
        raise RuntimeError("AUCTION_CACHE_META_KEY environment variable is required")
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )


async def open_redis() -> redis.Redis:

    global _shared_redis
    if _shared_redis is None:
        _shared_redis = create_redis_client()
    return _shared_redis


async def close_redis() -> None:

    global _shared_redis
    if _shared_redis is not None:
        await _shared_redis.aclose()
        _shared_redis = None


def get_redis() -> redis.Redis:

    if _shared_redis is None:
        raise RuntimeError("Redis is not initialized; call await open_redis() in app lifespan first")
    return _shared_redis


async def read_snapshot(client: redis.Redis) -> list[dict[str, Any]] | None:
    raw = await client.get(settings.AUCTION_CACHE_REDIS_KEY)
    if raw is None:
        logger.info("CACHE READ %s MISS", settings.AUCTION_CACHE_REDIS_KEY)
        return None
    data = json.loads(raw)
    if isinstance(data, list):
        logger.info("CACHE READ %s HIT items=%d", settings.AUCTION_CACHE_REDIS_KEY, len(data))
        return data
    logger.warning("CACHE READ %s HIT but payload is not a list", settings.AUCTION_CACHE_REDIS_KEY)
    return None


async def read_meta(client: redis.Redis) -> dict[str, str]:
    meta = await client.hgetall(settings.AUCTION_CACHE_META_KEY) or {}
    logger.info(
        "CACHE READ HGETALL %s count=%s updated_at=%s",
        settings.AUCTION_CACHE_META_KEY,
        meta.get("count"),
        meta.get("updated_at"),
    )
    return meta


async def load_cached_items(client: redis.Redis | None = None) -> list[dict[str, Any]] | None:

    r = client or get_redis()
    items = await read_snapshot(r)
    return items if items else None


def snapshot_at_iso(meta: dict[str, str]) -> str | None:
    raw = meta.get("updated_at")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
