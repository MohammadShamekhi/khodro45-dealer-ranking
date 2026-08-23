from __future__ import annotations

import os


RANKER_STRATEGY = os.getenv("RANKER_STRATEGY", )

PORT = int(os.getenv("PORT", ))

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_DB = int(os.getenv("REDIS_DB"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

AUCTION_CACHE_REDIS_KEY = os.getenv("AUCTION_CACHE_REDIS_KEY", "auctions:live:snapshot")
AUCTION_CACHE_META_KEY = os.getenv("AUCTION_CACHE_META_KEY", "auctions:live:meta")
DEALER_EXTRACTIONS_REDIS_KEY_PREFIX = os.getenv("DEALER_EXTRACTIONS_REDIS_KEY_PREFIX", "dealers_extractions")
DEALER_ENGAGEMENT_REDIS_KEY_PREFIX = os.getenv("DEALER_ENGAGEMENT_REDIS_KEY_PREFIX", "dealer_model_engagement")

LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", ).strip() or None
LOG_LEVEL = os.getenv("LOG_LEVEL", ).strip().upper()
