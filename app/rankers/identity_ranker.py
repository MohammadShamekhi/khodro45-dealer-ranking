from __future__ import annotations

from typing import Any

from app.ordering import _auction_id
from app.rankers.base import AuctionRanker, RankResult


class IdentityRanker(AuctionRanker):

    name = "identity"

    async def sort_auction_ids(
        self,
        *,
        dealer_id: int,
        auctions: list[dict[str, Any]],
    ) -> RankResult:
        _ = dealer_id
        out: list[int] = []
        for a in auctions:
            aid = _auction_id(a)
            if aid is not None:
                out.append(aid)
        return RankResult(auction_ids=out)
