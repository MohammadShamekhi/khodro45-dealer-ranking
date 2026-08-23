from __future__ import annotations

import json
import logging
from typing import Any

from app import settings
from app.auction_cache import get_redis, read_snapshot
from app.ordering import _auction_id
from app.rankers.base import AuctionRanker, RankResult
from app.rankers.identity_ranker import IdentityRanker

logger = logging.getLogger(__name__)

_PHASE_MODEL = 0
_PHASE_BRAND = 1
_PHASE_REST = 2
_ENGAGEMENT_SUMMARY_TOP_N = 100


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _auction_brand_model_ids(auction: dict[str, Any]) -> tuple[int | None, int | None]:

    trimyear = auction.get("trimyear") or {}
    trim = trimyear.get("trim") or {}
    model = trim.get("model") or {}
    brand = model.get("brand") or {}
    return _as_int(brand.get("id")), _as_int(model.get("id"))


def _dealer_engagement_redis_key(dealer_id: int) -> str:
    return f"{settings.DEALER_ENGAGEMENT_REDIS_KEY_PREFIX}:{dealer_id}"


async def _load_engagement_payload(dealer_id: int) -> tuple[str, dict[str, Any] | None]:
    key = _dealer_engagement_redis_key(dealer_id)
    client = get_redis()
    raw = await client.get(key)
    if raw is None:
        logger.info("GET %s miss dealer_id=%d", key, dealer_id)
        return key, None
    payload = json.loads(raw)
    logger.info("GET %s hit dealer_id=%d", key, dealer_id)

    return key, payload


def _engagement_ranks(payload: dict[str, Any]) -> tuple[dict[int, int], dict[int, int]]:
    """
    Brand rank equals the best model rank of that brand: if a model is rank 1, its brand
    is rank 1 in the brand phase. If several models of the same brand appear, the brand
    keeps the topmost (smallest) of those model ranks.
    """
    model_ranks: dict[int, int] = {}
    brand_ranks: dict[int, int] = {}

    for entry in payload.get("scores") or []:
        if not isinstance(entry, dict):
            continue
        model_id = _as_int(entry.get("model_id"))
        brand_id = _as_int(entry.get("brand_id"))
        rank = _as_int(entry.get("rank"))
        if model_id is None or rank is None:
            continue

        if model_id not in model_ranks or rank < model_ranks[model_id]:
            model_ranks[model_id] = rank

        if brand_id is not None and (brand_id not in brand_ranks or rank < brand_ranks[brand_id]):
            brand_ranks[brand_id] = rank

    return model_ranks, brand_ranks


async def read_dealer_engagement(
    dealer_id: int,
) -> tuple[dict[int, int], dict[int, int], bool]:

    key, payload = await _load_engagement_payload(dealer_id)
    if payload is None:
        return {}, {}, False
    model_ranks, brand_ranks = _engagement_ranks(payload)
    logger.info(
        "GET %s hit dealer_id=%d models=%d brands=%d",
        key,
        dealer_id,
        len(model_ranks),
        len(brand_ranks),
    )
    return model_ranks, brand_ranks, True



def _score_entry_summary(entry: dict[str, Any]) -> dict[str, Any] | None:


    rank = _as_int(entry.get("rank"))
    model_id = _as_int(entry.get("model_id"))
    if rank is None or model_id is None:
        return None
    try:
        score = float(entry.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return {
        "rank": rank,
        "score": score,
        "brand_id": _as_int(entry.get("brand_id")),
        "model_id": model_id,
        "brand_title": entry.get("brand_title"),
        "model_title": entry.get("model_title"),
        "brand_title_english": entry.get("brand_title_english"),
        "model_title_english": entry.get("model_title_english"),
    }


async def read_dealer_engagement_summary(
    dealer_id: int,
    *,
    top_n: int = _ENGAGEMENT_SUMMARY_TOP_N,
) -> dict[str, Any]:

    key, payload = await _load_engagement_payload(dealer_id)
    if payload is None:
        return {
            "dealer_id": dealer_id,
            "redis_key": key,
            "found": False,
            "updated_at": None,
            "total_models": 0,
            "total_brands": 0,
            "scores": [],
            "brands": [],
        }

    _model_ranks, brand_ranks = _engagement_ranks(payload)

    rows: list[dict[str, Any]] = []
    for entry in payload.get("scores") or []:
        if not isinstance(entry, dict):
            continue
        summary = _score_entry_summary(entry)
        if summary is None:
            continue
        brand_id = summary.get("brand_id")
        summary["brand_rank"] = brand_ranks.get(brand_id) if brand_id is not None else None
        rows.append(summary)

    rows.sort(key=lambda row: (row["rank"], row["model_id"]))
    top_rows = rows[: max(0, top_n)]

    brands_by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        brand_id = row.get("brand_id")
        brand_rank = row.get("brand_rank")
        if brand_id is None or brand_rank is None:
            continue
        if brand_id not in brands_by_id:
            brands_by_id[brand_id] = {
                "brand_rank": brand_rank,
                "brand_id": brand_id,
                "brand_title": row.get("brand_title"),
                "brand_title_english": row.get("brand_title_english"),
            }
    brands = sorted(brands_by_id.values(), key=lambda b: (b["brand_rank"], b["brand_id"]))

    return {
        "dealer_id": dealer_id,
        "redis_key": key,
        "found": True,
        "updated_at": payload.get("updated_at"),
        "total_models": len(rows), 
        "total_brands": len(brands),
        "scores": top_rows,
        "brands": brands[: max(0, top_n)],
    }


def _sort_key(
    auction: dict[str, Any],
    *,
    model_ranks: dict[int, int],
    brand_ranks: dict[int, int],
) -> tuple[int, int, int] | None:
    """
    Phase 0 — model match: ordered by model rank (1, 2, …), then auction_id.
    Phase 1 — brand match (no model match): ordered by brand rank, then auction_id.
    Phase 2 — neither: ordered by auction_id only (rank ignored).
    """
    aid = _auction_id(auction)
    if aid is None:
        return None

    brand_id, model_id = _auction_brand_model_ids(auction)

    if model_id is not None and model_id in model_ranks:
        return (_PHASE_MODEL, model_ranks[model_id], aid)
    if brand_id is not None and brand_id in brand_ranks:
        return (_PHASE_BRAND, brand_ranks[brand_id], aid)
    return (_PHASE_REST, 0, aid)


class EngagementRanker(AuctionRanker):
    """
    ``RankResult`` flags are reused as personalization/fallback signals:
    ``preference_called`` = engagement applied, ``dealer_not_found`` = no cache entry,
    ``preferences_empty`` = cache entry present but no usable ranks.
    """

    name = "engagement"

    async def sort_auction_ids(
        self,
        *,
        dealer_id: int,
        auctions: list[dict[str, Any]],
    ) -> RankResult:
        items = auctions
        if not items:
            client = get_redis()
            snapshot = await read_snapshot(client)
            items = snapshot or []

        model_ranks, brand_ranks, found = await read_dealer_engagement(dealer_id)

        if not found or not model_ranks:
            fallback_reason = "dealer_not_found_in_model_engagement" if not found else "empty_engagement"
            logger.info(
                "dealer_id=%d engagement unavailable; deferring to identity ranker",
                dealer_id,
                extra={
                    "dealer_id": dealer_id,
                    "engagement_found": found,
                    "ranking_mode": "identity",
                    "fallback_reason": fallback_reason,
                    "auction_count": len(items),
                },
            )
            identity = await IdentityRanker().sort_auction_ids(dealer_id=dealer_id, auctions=items)
            return RankResult(
                auction_ids=identity.auction_ids,
                preference_called=False,
                dealer_not_found=not found,
                preferences_empty=found and not model_ranks,
            )

        logger.info(
            "dealer_id=%d engagement found; applied engagement ranking",
            dealer_id,
            extra={
                "dealer_id": dealer_id,
                "engagement_found": True,
                "ranking_mode": "engagement",
                "model_count": len(model_ranks),
                "brand_count": len(brand_ranks),
                "auction_count": len(items),
            },
        )

        ranked: list[tuple[int, int, int]] = []
        for auction in items:
            key = _sort_key(auction, model_ranks=model_ranks, brand_ranks=brand_ranks)
            if key is not None:
                ranked.append(key)

        ranked.sort()
        return RankResult(
            auction_ids=[aid for _phase, _rank, aid in ranked],
            preference_called=True,
            dealer_not_found=False,
            preferences_empty=False,
        )
