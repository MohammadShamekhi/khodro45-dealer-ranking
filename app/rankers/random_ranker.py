from __future__ import annotations

import random
from typing import Any

from app.ordering import _auction_id
from app.rankers.base import AuctionRanker, RankResult


class RandomRanker(AuctionRanker):

    name = "random"

    async def sort_auction_ids(
        self,
        *,
        dealer_id: int,
        auctions: list[dict[str, Any]],
    ) -> RankResult:
        ids: list[int] = []
        for auction in auctions:
            aid = _auction_id(auction)
            if aid is not None:
                ids.append(aid)
        rng = random.Random(dealer_id)
        rng.shuffle(ids)
        return RankResult(auction_ids=ids)
