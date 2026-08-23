from __future__ import annotations
import os
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from toolbox.logger import configure_logging, get_logger

from app import settings
from app.auction_cache import (
    close_redis,
    get_redis,
    load_cached_items,
    open_redis,
    read_meta,
    read_snapshot,
    snapshot_at_iso,
)
from app.ordering import reorder_by_ids
from app.rankers import get_ranker
from app.rankers.base import RankResult
from app.rankers.identity_ranker import IdentityRanker
from app.rankers.engagement_ranker import read_dealer_engagement_summary
from app.rankers.preference_ranker import read_dealer_extraction_summary
from dealer_preferences.api import router as dealer_preferences_router

logger = get_logger(__name__)

_PLAYGROUND_HTML = Path(__file__).resolve().parent / "static" / "index.html"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging()
    logging.getLogger("uvicorn.access").disabled = True
    logger.info("ranker strategy=%s", settings.RANKER_STRATEGY)

    await open_redis()
    logger.info(
        "redis connected %s:%s/%s",
        settings.REDIS_HOST,
        settings.REDIS_PORT,
        settings.REDIS_DB,
    )

    yield

    await close_redis()
    logger.info("redis connection closed")


app = FastAPI(title="Dealer Intelligence", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Dealer-Extraction", "X-Dealer-Engagement"],
)

app.include_router(dealer_preferences_router, prefix="/api/v1/dealer_preferences")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def playground() -> HTMLResponse:
    return HTMLResponse(_PLAYGROUND_HTML.read_text(encoding="utf-8"))


def _dealer_id(x_dealer_id: str | None) -> int | None:
    if x_dealer_id and x_dealer_id.strip():
        try:
            return int(x_dealer_id.strip())
        except ValueError:
            return None
    return None


def _require_dealer_id(x_dealer_id: str | None) -> int | JSONResponse:
    dealer_id = _dealer_id(x_dealer_id)
    if dealer_id is None:
        return JSONResponse(
            status_code=400,
            content={"detail": "X-Dealer-Id header required"},
        )
    return dealer_id


async def _items_from_redis() -> list[dict[str, Any]] | None:
    return await load_cached_items(get_redis())


def _empty_snapshot_response() -> JSONResponse:

    return JSONResponse(
        status_code=503,
        content={"detail": "no auction snapshot in redis yet"},
    )


async def _dealer_playground_headers(dealer_id: int | None) -> dict[str, str]:

    if dealer_id is None:
        return {}

    extraction = await read_dealer_extraction_summary(dealer_id)
    engagement = await read_dealer_engagement_summary(dealer_id)
    return {
        "X-Dealer-Extraction": json.dumps(extraction, ensure_ascii=True),
        "X-Dealer-Engagement": json.dumps(engagement, ensure_ascii=True),
    }


def _json_response(
    content: Any,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code, headers=headers or {})


def _elapsed_ms(start: float, end: float | None = None) -> float:
    return round(((end if end is not None else time.perf_counter()) - start) * 1000, 2)


async def _sort_auction_ids(
    *,
    dealer_id: int,
    items: list[dict[str, Any]],
) -> RankResult:

    ranker = get_ranker()
    return await ranker.sort_auction_ids(dealer_id=dealer_id, auctions=items)


async def _sorted_ids_with_fallback(
    *,
    dealer_id: int,
    items: list[dict[str, Any]],
) -> RankResult:
    try:
        return await _sort_auction_ids(dealer_id=dealer_id, items=items)
    except Exception as e:
        logger.exception("ranker failed; falling back to identity order: %s", e)
        result = await IdentityRanker().sort_auction_ids(dealer_id=dealer_id, auctions=items)
        logger.info(
            "ranker fallback dealer_id=%d ids=%d",
            dealer_id,
            len(result.auction_ids),
            extra={
                "dealer_id": dealer_id,
                "auction_count": len(result.auction_ids),
                "auction_ids": result.auction_ids,
                "ranker_strategy": settings.RANKER_STRATEGY,
                "ranking_mode": "identity",
                "fallback_reason": "ranker_exception",
            },
        )
        return result


async def _suggested_from_cache(
    x_dealer_id: str | None,
) -> tuple[int, RankResult, list[dict[str, Any]], dict[str, str], dict[str, float]] | JSONResponse:

    t0 = time.perf_counter()
    dealer_id = _require_dealer_id(x_dealer_id)
    if isinstance(dealer_id, JSONResponse):
        return dealer_id

    client = get_redis()
    items = await read_snapshot(client)
    t_snapshot = time.perf_counter()
    meta = await read_meta(client)
    t_meta = time.perf_counter()

    if not items:
        return _empty_snapshot_response()

    rank_result = await _sorted_ids_with_fallback(dealer_id=dealer_id, items=items)
    hardcoded_auction_id = int(os.getenv("HARDCODED_AUCTION_ID")) or None
    if hardcoded_auction_id and hardcoded_auction_id in rank_result.auction_ids:
        rank_result.auction_ids.remove(hardcoded_auction_id)
        rank_result.auction_ids.insert(0, hardcoded_auction_id)

    t_rank = time.perf_counter()
    timings = {
        "redis_snapshot_ms": _elapsed_ms(t0, t_snapshot),
        "redis_meta_ms": _elapsed_ms(t_snapshot, t_meta),
        "rank_ms": _elapsed_ms(t_meta, t_rank),
        "cache_rank_ms": _elapsed_ms(t0, t_rank),
    }
    return dealer_id, rank_result, items, meta, timings


@app.get("/api/v1/live/")
async def live(x_dealer_id: str | None = Header(default=None, alias="X-Dealer-Id")):
    """Live auction list from Redis (default end_time order)."""
    dealer_id = _dealer_id(x_dealer_id)
    items = await _items_from_redis()
    if not items:
        return _empty_snapshot_response()
    logger.info(
        "GET /api/v1/live/ dealer_id=%s items=%d",
        dealer_id,
        len(items),
        extra={
            "endpoint": "/api/v1/live/",
            "dealer_id": dealer_id,
            "auction_count": len(items),
        },
    )
    extraction_headers = await _dealer_playground_headers(dealer_id)
    return _json_response(items, headers=extraction_headers)


@app.get("/api/v1/suggested_ids/")
async def suggested_ids(x_dealer_id: str | None = Header(default=None, alias="X-Dealer-Id")):

    t0 = time.perf_counter()
    result = await _suggested_from_cache(x_dealer_id)
    if not isinstance(result, tuple):
        return result

    dealer_id, rank_result, _items, _meta, timings = result
    auction_ids = rank_result.auction_ids
    t_extraction = time.perf_counter()
    extraction_headers = await _dealer_playground_headers(dealer_id)
    timings["extraction_ms"] = _elapsed_ms(t_extraction)
    timings["request_ms"] = _elapsed_ms(t0)
    logger.info(
        "GET /api/v1/suggested_ids/ dealer_id=%d ids=%d",
        dealer_id,
        len(auction_ids),
        extra={
            "endpoint": "/api/v1/suggested_ids/",
            "dealer_id": dealer_id,
            "auction_count": len(auction_ids),
            "auction_ids": auction_ids,
            "ranker_strategy": settings.RANKER_STRATEGY,
            "preference_called": rank_result.preference_called,
            "dealer_not_found": rank_result.dealer_not_found,
            "preferences_empty": rank_result.preferences_empty,
            **timings,
        },
    )
    return _json_response(
        {"auction_ids": auction_ids},
        headers=extraction_headers,
    )


@app.get("/api/v1/suggested_full/")
async def suggested_full(x_dealer_id: str | None = Header(default=None, alias="X-Dealer-Id")):

    t0 = time.perf_counter()
    result = await _suggested_from_cache(x_dealer_id)
    if not isinstance(result, tuple):
        return result

    dealer_id, rank_result, items, meta, timings = result
    auction_ids = rank_result.auction_ids
    ranked_items = reorder_by_ids(items, auction_ids)
    t_extraction = time.perf_counter()
    extraction_headers = await _dealer_playground_headers(dealer_id)
    timings["extraction_ms"] = _elapsed_ms(t_extraction)
    timings["request_ms"] = _elapsed_ms(t0)
    logger.info(
        "GET /api/v1/suggested_full/ dealer_id=%d items=%d snapshot_at=%s",
        dealer_id,
        len(ranked_items),
        snapshot_at_iso(meta),
        extra={
            "endpoint": "/api/v1/suggested_full/",
            "dealer_id": dealer_id,
            "auction_count": len(auction_ids),
            "auction_ids": auction_ids,
            "ranker_strategy": settings.RANKER_STRATEGY,
            "preference_called": rank_result.preference_called,
            "dealer_not_found": rank_result.dealer_not_found,
            "preferences_empty": rank_result.preferences_empty,
            **timings,
        },
    )
    return _json_response(ranked_items, headers=extraction_headers)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ranker_strategy": settings.RANKER_STRATEGY}
