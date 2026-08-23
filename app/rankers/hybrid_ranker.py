from __future__ import annotations

import logging
from typing import Any

from app.auction_cache import get_redis, read_snapshot
from app.ordering import _auction_id
from app.rankers.base import AuctionRanker, RankResult
from app.rankers.engagement_ranker import EngagementRanker
from app.rankers.preference_ranker import (
    _TIER_REST,
    _auction_brand_model,
    _match_tier,
    read_dealer_preferences,
)

logger = logging.getLogger(__name__)


class HybridRanker(AuctionRanker):

    name = "hybrid"

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
                "dealer_id=%d preferences unavailable; deferring to engagement ranker",
                dealer_id,
                extra={
                    "dealer_id": dealer_id,
                    "preferences_found": preferences_found,
                    "ranking_mode": "engagement",
                    "fallback_reason": fallback_reason,
                    "auction_count": len(items),
                },
            )
            return await EngagementRanker().sort_auction_ids(dealer_id=dealer_id, auctions=items)

        preferred: list[tuple[int, int]] = []
        unmatched: list[dict[str, Any]] = []

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
            if tier == _TIER_REST:
                unmatched.append(auction)
            else:
                preferred.append((tier, aid))

        preferred.sort()
        preferred_ids = [aid for _tier, aid in preferred]

        engagement = await EngagementRanker().sort_auction_ids(
            dealer_id=dealer_id,
            auctions=unmatched,
        )
        unmatched_ids = engagement.auction_ids

        logger.info(
            "dealer_id=%d hybrid ranking applied",
            dealer_id,
            extra={
                "dealer_id": dealer_id,
                "ranking_mode": "hybrid",
                "preference_count": len(preferred_ids),
                "engagement_count": len(unmatched_ids),
                "brand_count": len(pref_brands),
                "model_count": len(pref_models),
                "auction_count": len(items),
            },
        )

        return RankResult(
            auction_ids=preferred_ids + unmatched_ids,
            preference_called=True,
            dealer_not_found=False,
            preferences_empty=False,
        )
