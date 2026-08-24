from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from app.auction_cache import get_redis, read_snapshot
from app.catboost_feature_cache import FEATURE_COLUMNS
from app.ordering import _auction_id
from app.rankers.ascending_id_ranker import AscendingIdRanker
from app.rankers.base import AuctionRanker, RankResult

logger = logging.getLogger(__name__)


MODEL_FEATURE_COLUMNS = [
    "dealer_id",
    "brand_id",
    "model_id",
    *FEATURE_COLUMNS,
]


def _as_int(value: Any) -> int | None:
    """Convert a value to int when possible."""
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _auction_brand_model_ids(
    auction: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Extract brand and model IDs from an auction."""
    trimyear = auction.get("trimyear") or {}
    trim = trimyear.get("trim") or {}
    model = trim.get("model") or {}
    brand = model.get("brand") or {}

    brand_id = _as_int(brand.get("id"))
    model_id = _as_int(model.get("id"))

    return brand_id, model_id


def _brand_model_key(
    brand_id: int,
    model_id: int,
) -> str:
    """Build the feature cache lookup key."""
    return f"{brand_id}:{model_id}"


def _parse_dealer_features(
    raw_payload: Any,
) -> dict[str, dict[str, Any]] | None:
    """Parse and validate the dealer feature payload."""
    if raw_payload is None:
        return None

    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    parsed_payload: dict[str, dict[str, Any]] = {}

    for feature_key, feature_values in payload.items():
        if not isinstance(feature_key, str):
            continue

        if not isinstance(feature_values, dict):
            continue

        parsed_payload[feature_key] = feature_values

    return parsed_payload


async def _load_dealer_features(
    dealer_id: int,
) -> dict[str, dict[str, Any]] | None:
    """Load all feature groups for one dealer with a single Redis GET."""
    redis_client = get_redis()
    redis_key = str(dealer_id)

    raw_payload = await redis_client.get(redis_key)

    if raw_payload is None:
        logger.info(
            "GET dealer features miss dealer_id=%d",
            dealer_id,
        )
        return None

    feature_payload = _parse_dealer_features(raw_payload)

    if feature_payload is None:
        logger.warning(
            "GET dealer features returned invalid payload dealer_id=%d",
            dealer_id,
        )
        return None

    logger.info(
        "GET dealer features hit dealer_id=%d feature_groups=%d",
        dealer_id,
        len(feature_payload),
    )

    return feature_payload


def _build_feature_row(
    *,
    dealer_id: int,
    brand_id: int,
    model_id: int,
    cached_features: dict[str, Any],
) -> dict[str, Any] | None:
    """Build one model input row from auction and cached features."""
    missing_columns = [
        column
        for column in FEATURE_COLUMNS
        if column not in cached_features
    ]

    if missing_columns:
        return None

    return {
        "dealer_id": dealer_id,
        "brand_id": brand_id,
        "model_id": model_id,
        **{
            column: cached_features[column]
            for column in FEATURE_COLUMNS
        },
    }


def _build_model_inputs(
    *,
    dealer_id: int,
    auctions: list[dict[str, Any]],
    dealer_features: dict[str, dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[int, int]],
    list[int],
]:
    """Build model rows and keep scored/unscored auction mappings."""
    rows: list[dict[str, Any]] = []
    scored_items: list[tuple[int, int]] = []
    unscored_auction_ids: list[int] = []

    for original_index, auction in enumerate(auctions):
        auction_id = _auction_id(auction)

        if auction_id is None:
            continue

        brand_id, model_id = _auction_brand_model_ids(auction)

        if brand_id is None or model_id is None:
            unscored_auction_ids.append(int(auction_id))
            continue

        feature_key = _brand_model_key(
            brand_id=brand_id,
            model_id=model_id,
        )

        cached_features = dealer_features.get(feature_key)

        if cached_features is None:
            unscored_auction_ids.append(int(auction_id))
            continue

        row = _build_feature_row(
            dealer_id=dealer_id,
            brand_id=brand_id,
            model_id=model_id,
            cached_features=cached_features,
        )

        if row is None:
            logger.warning(
                "Incomplete CatBoost features auction_id=%d feature_key=%s",
                auction_id,
                feature_key,
            )
            unscored_auction_ids.append(int(auction_id))
            continue

        rows.append(row)
        scored_items.append(
            (
                int(auction_id),
                original_index,
            )
        )

    return rows, scored_items, unscored_auction_ids


def _predict_scores(
    model: Any,
    rows: list[dict[str, Any]],
) -> list[float]:
    """Create the model dataframe and return positive-class probabilities."""
    frame = pd.DataFrame(
        rows,
        columns=MODEL_FEATURE_COLUMNS,
    )

    frame = frame.apply(
        pd.to_numeric,
        errors="coerce",
    )

    if frame.isna().any().any():
        raise ValueError(
            "CatBoost feature dataframe contains invalid numeric values"
        )

    probabilities = model.predict_proba(frame)

    if len(probabilities) != len(rows):
        raise ValueError(
            "Prediction count does not match input row count"
        )

    scores: list[float] = []

    for probability in probabilities:
        if len(probability) < 2:
            raise ValueError(
                "CatBoost predict_proba returned invalid probabilities"
            )

        scores.append(float(probability[1]))

    return scores


def _sort_scored_items(
    *,
    scored_items: list[tuple[int, int]],
    scores: list[float],
) -> list[int]:
    """Sort scored auctions by probability in descending order."""
    ranked_items = [
        (
            score,
            original_index,
            auction_id,
        )
        for (auction_id, original_index), score in zip(
            scored_items,
            scores,
        )
    ]

    ranked_items.sort(
        key=lambda item: (
            -item[0],
            item[1],
            item[2],
        )
    )

    return [
        auction_id
        for _score, _original_index, auction_id in ranked_items
    ]


async def _ascending_fallback(
    *,
    dealer_id: int,
    auctions: list[dict[str, Any]],
    reason: str,
    dealer_not_found: bool = False,
    preferences_empty: bool = False,
) -> RankResult:
    """Return the standard ascending ID fallback result."""
    logger.info(
        "CatBoost fallback dealer_id=%d reason=%s",
        dealer_id,
        reason,
        extra={
            "dealer_id": dealer_id,
            "ranking_mode": "ascending_id",
            "fallback_reason": reason,
            "auction_count": len(auctions),
        },
    )

    fallback_result = await AscendingIdRanker().sort_auction_ids(
        dealer_id=dealer_id,
        auctions=auctions,
    )

    return RankResult(
        auction_ids=fallback_result.auction_ids,
        preference_called=False,
        dealer_not_found=dealer_not_found,
        preferences_empty=preferences_empty,
    )


class CatBoostRanker(AuctionRanker):
    name = "catboost"

    def __init__(self, model: Any) -> None:
        self.model = model

    async def sort_auction_ids(
        self,
        *,
        dealer_id: int,
        auctions: list[dict[str, Any]],
    ) -> RankResult:
        items = auctions

        if not items:
            redis_client = get_redis()
            snapshot = await read_snapshot(redis_client)
            items = snapshot or []

        if not items:
            return RankResult(auction_ids=[])

        try:
            dealer_features = await _load_dealer_features(dealer_id)
        except Exception:
            logger.exception(
                "Failed to load CatBoost features dealer_id=%d",
                dealer_id,
            )

            return await _ascending_fallback(
                dealer_id=dealer_id,
                auctions=items,
                reason="catboost_feature_cache_error",
                preferences_empty=True,
            )

        if dealer_features is None:
            return await _ascending_fallback(
                dealer_id=dealer_id,
                auctions=items,
                reason="dealer_not_found",
                dealer_not_found=True,
            )

        if not dealer_features:
            return await _ascending_fallback(
                dealer_id=dealer_id,
                auctions=items,
                reason="empty_features",
                preferences_empty=True,
            )

        rows, scored_items, unscored_auction_ids = _build_model_inputs(
            dealer_id=dealer_id,
            auctions=items,
            dealer_features=dealer_features,
        )

        if not rows:
            return await _ascending_fallback(
                dealer_id=dealer_id,
                auctions=items,
                reason="no_matching_features",
                preferences_empty=True,
            )

        try:
            scores = _predict_scores(
                model=self.model,
                rows=rows,
            )
        except Exception:
            logger.exception(
                "CatBoost prediction failed dealer_id=%d",
                dealer_id,
            )

            return await _ascending_fallback(
                dealer_id=dealer_id,
                auctions=items,
                reason="catboost_prediction_error",
                preferences_empty=True,
            )

        ranked_auction_ids = _sort_scored_items(
            scored_items=scored_items,
            scores=scores,
        )

        unscored_auction_ids.sort()
        ranked_auction_ids.extend(unscored_auction_ids)

        logger.info(
            "CatBoost ranking applied dealer_id=%d",
            dealer_id,
            extra={
                "dealer_id": dealer_id,
                "ranking_mode": "catboost",
                "auction_count": len(items),
                "scored_count": len(scored_items),
                "unscored_count": len(unscored_auction_ids),
            },
        )

        return RankResult(
            auction_ids=ranked_auction_ids,
            preference_called=True,
            dealer_not_found=False,
            preferences_empty=False,
        )
