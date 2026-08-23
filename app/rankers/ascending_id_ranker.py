from __future__ import annotations

from typing import Any

from app.ordering import _auction_id
from app.rankers.base import AuctionRanker, RankResult


class AscendingIdRanker(AuctionRanker):

    name = "ascending_id"

    async def sort_auction_ids(
        self,
        *,
        dealer_id: int,
        auctions: list[dict[str, Any]],
    ) -> RankResult:
        _ = dealer_id
        ids: list[int] = []
        for auction in auctions:
            aid = _auction_id(auction)
            if aid is not None:
                ids.append(aid)
        ids.sort()
        return RankResult(auction_ids=ids)
