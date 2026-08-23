from __future__ import annotations

import json
import logging
from typing import Any

from app import settings
from app.auction_cache import get_redis, read_snapshot
from app.ordering import _auction_id
from app.rankers.base import AuctionRanker, RankResult

logger = logging.getLogger(__name__)

_TIER_BRAND_MODEL = 0
_TIER_MODEL = 1
_TIER_BRAND = 2
_TIER_REST = 3


def _norm_title(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _title_set(values: Any) -> set[str]:

    if not isinstance(values, list):
        return set()
    out: set[str] = set()
    for value in values:
        title = _norm_title(value)
        if title is not None:
            out.add(title)
    return out


def _auction_brand_model(auction: dict[str, Any]) -> tuple[str | None, str | None]:

    trimyear = auction.get("trimyear") or {}
    trim = trimyear.get("trim") or {}
    model = trim.get("model") or {}
    brand = model.get("brand") or {}
    return _norm_title(brand.get("title")), _norm_title(model.get("title"))


def _parse_dealer_extraction(payload: dict[str, Any]) -> tuple[set[str], set[str]]:

    nested = payload.get("extractions")
    if not isinstance(nested, dict):
        return set(), set()
    return _title_set(nested.get("brand")), _title_set(nested.get("model"))


def _extract_comment(payload: dict[str, Any]) -> str | None:

    nested = payload.get("extractions")
    if not isinstance(nested, dict) or nested.get("comment") is None:
        return None
    text = str(nested.get("comment")).strip()
    return text or None


def _dealer_extraction_redis_key(dealer_id: int) -> str:
    return f"{settings.DEALER_EXTRACTIONS_REDIS_KEY_PREFIX}:{dealer_id}"


async def _load_dealer_extraction_payload(dealer_id: int) -> tuple[str, dict[str, Any] | None]:
    key = _dealer_extraction_redis_key(dealer_id)
    client = get_redis()
    raw = await client.get(key)
    if raw is None:
        logger.info("GET %s miss dealer_id=%d", key, dealer_id)
        return key, None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("GET %s hit but payload is not valid JSON", key)
        return key, None
    if not isinstance(payload, dict):
        logger.warning("GET %s hit but payload is not an object", key)
        return key, None
    return key, payload


async def read_dealer_extraction_summary(dealer_id: int) -> dict[str, Any]:
    
    key, payload = await _load_dealer_extraction_payload(dealer_id)
    if payload is None:
        return {
            "dealer_id": dealer_id,
            "redis_key": key,
            "found": False,
            "comment": None,
            "brand": [],
            "model": [],
        }
    brands, models = _parse_dealer_extraction(payload)
    return {
        "dealer_id": dealer_id,
        "redis_key": key,
        "found": True,
        "comment": _extract_comment(payload),
        "brand": sorted(brands),
        "model": sorted(models),
    }


def _match_tier(
    *,
    auction_brand: str | None,
    auction_model: str | None,
    pref_brands: set[str],
    pref_models: set[str],
) -> int:
    brand_match = auction_brand is not None and auction_brand in pref_brands
    model_match = auction_model is not None and auction_model in pref_models

    if pref_brands and pref_models and brand_match and model_match:
        return _TIER_BRAND_MODEL
    if model_match:
        return _TIER_MODEL
    if brand_match:
        return _TIER_BRAND
    return _TIER_REST


async def read_dealer_preferences(dealer_id: int) -> tuple[set[str], set[str], bool]:
    
    key, payload = await _load_dealer_extraction_payload(dealer_id)
    if payload is None:
        return set(), set(), False
    brands, models = _parse_dealer_extraction(payload)
    logger.info(
        "GET %s hit dealer_id=%d brands=%d models=%d",
        key,
        dealer_id,
        len(brands),
        len(models),
    )
    return brands, models, True


class PreferenceRanker(AuctionRanker):

    name = "preference"

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

        pref_brands, pref_models, preferences_found = await read_dealer_preferences(dealer_id)
        if not preferences_found or (not pref_brands and not pref_models):
            fallback_reason = "dealer_not_found" if not preferences_found else "empty_preferences"
            logger.info(
                "dealer_id=%d preferences not found; returning ascending id order",
                dealer_id,
                extra={
                    "dealer_id": dealer_id,
                    "preferences_found": False,
                    "ranking_mode": "ascending_id",
                    "fallback_reason": fallback_reason,
                    "auction_count": len(items),
                },
            )
            return RankResult(
                auction_ids=_ascending_id_order(items),
                preference_called=False,
                dealer_not_found=not preferences_found,
                preferences_empty=preferences_found and not pref_brands and not pref_models,
            )

        logger.info(
            "dealer_id=%d preferences found; applied preference ranking",
            dealer_id,
            extra={
                "dealer_id": dealer_id,
                "preferences_found": True,
                "ranking_mode": "preference",
                "brand_count": len(pref_brands),
                "model_count": len(pref_models),
                "auction_count": len(items),
            },
        )

        ranked: list[tuple[int, int]] = []
        for auction in items:
            aid = _auction_id(auction)
            if aid is None:
                continue
            brand, model = _auction_brand_model(auction)
            tier = _match_tier(
                auction_brand=brand,
                auction_model=model,
                pref_brands=pref_brands,
                pref_models=pref_models,
            )
            ranked.append((tier, aid))

        ranked.sort()
        return RankResult(
            auction_ids=[aid for _tier, aid in ranked],
            preference_called=True,
            dealer_not_found=False,
            preferences_empty=False,
        )


def _ascending_id_order(auctions: list[dict[str, Any]]) -> list[int]:
    out: list[int] = []
    for auction in auctions:
        aid = _auction_id(auction)
        if aid is not None:
            out.append(aid)
    out.sort()
    return out
