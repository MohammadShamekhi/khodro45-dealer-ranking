from __future__ import annotations

import logging

from app import settings
from app.rankers.ascending_id_ranker import AscendingIdRanker
from app.rankers.base import AuctionRanker
from app.rankers.engagement_ranker import EngagementRanker
from app.rankers.hybrid_ranker import HybridRanker
from app.rankers.identity_ranker import IdentityRanker
from app.rankers.preference_ranker import PreferenceRanker
from app.rankers.random_ranker import RandomRanker

logger = logging.getLogger(__name__)

# Register ranker implementations here. Add new model classes as they ship.
_STRATEGIES: dict[str, type[AuctionRanker]] = {
    "ascending_id": AscendingIdRanker,
    "engagement": EngagementRanker,
    "hybrid": HybridRanker,
    "identity": IdentityRanker,
    "preference": PreferenceRanker,
    "random": RandomRanker,
}


def get_ranker() -> AuctionRanker:
    name = settings.RANKER_STRATEGY.strip().lower()
    cls = _STRATEGIES.get(name)
    if cls is None:
        known = ", ".join(sorted(_STRATEGIES))
        raise ValueError(f"Unknown RANKER_STRATEGY={name!r}; known: {known}")
    logger.debug("using ranker strategy=%s class=%s", name, cls.__name__)
    return cls()
