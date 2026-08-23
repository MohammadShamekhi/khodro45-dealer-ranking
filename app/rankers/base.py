from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RankResult:
    auction_ids: list[int]
    preference_called: bool = False
    dealer_not_found: bool = False
    preferences_empty: bool = False


class AuctionRanker(ABC):

    name: str = "base"

    @abstractmethod

    async def sort_auction_ids(
        self,
        *,
        dealer_id: int,
        auctions: list[dict[str, Any]],
    ) -> RankResult:
        """Return ranked auction ids and metadata."""
