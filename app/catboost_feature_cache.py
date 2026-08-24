from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import redis.asyncio as redis

logger = logging.getLogger(__name__)


ID_COLUMNS = [
    "dealer_id",
    "brand_id",
    "model_id",
]

FEATURE_COLUMNS = [
    "days_since_last_view",
    "days_since_last_bid",
    "bid_view_ratio",
    "global_bid_view_ratio",
    "views_3d",
    "views_7d",
    "views_14d",
    "bids_3d",
    "bids_7d",
    "bids_14d",
]

REQUIRED_COLUMNS = ID_COLUMNS + FEATURE_COLUMNS


def _to_json_value(value: Any) -> int | float | str | bool | None:
    if pd.isna(value):
        return 0

    if hasattr(value, "item"):
        value = value.item()

    if isinstance(value, (int, float, str, bool)):
        return value

    return str(value)


def _make_brand_model_key(
    brand_id: int,
    model_id: int,
) -> str:
    return f"{brand_id}:{model_id}"


def _build_feature_payload(
    row: pd.Series,
) -> dict[str, Any]:
    return {
        column: _to_json_value(row[column])
        for column in FEATURE_COLUMNS
    }


def _build_dealer_payload(
    dealer_df: pd.DataFrame,
) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}

    for _, row in dealer_df.iterrows():
        brand_model_key = _make_brand_model_key(
            brand_id=int(row["brand_id"]),
            model_id=int(row["model_id"]),
        )

        payload[brand_model_key] = _build_feature_payload(row)

    return payload


def _prepare_feature_dataframe(
    csv_path: str | Path,
) -> tuple[pd.DataFrame, int]:
    path = Path(csv_path)

    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {path}")

    df = pd.read_csv(path)

    missing_columns = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing_columns:
        raise ValueError(
            f"Missing required CSV columns: {sorted(missing_columns)}"
        )

    df = df.dropna(subset=ID_COLUMNS)

    duplicate_mask = df.duplicated(
        subset=ID_COLUMNS,
        keep="last",
    )

    duplicate_count = int(duplicate_mask.sum())

    if duplicate_count:
        logger.warning(
            "Duplicate dealer/brand/model rows found; keeping the last row",
            extra={
                "duplicate_count": duplicate_count,
            },
        )
        df = df.loc[~duplicate_mask]

    return df, duplicate_count


async def cache_dealer_features_to_redis(
    csv_path: str | Path,
    redis_client: redis.Redis,
    *,
    batch_size: int = 500,
    ttl_seconds: int | None = None,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    if ttl_seconds is not None and ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be greater than zero")

    df, duplicate_count = _prepare_feature_dataframe(csv_path)

    cached_dealers = 0
    cached_rows = 0

    async with redis_client.pipeline(transaction=False) as pipe:
        for dealer_id, dealer_df in df.groupby(
            "dealer_id",
            sort=False,
        ):
            dealer_key = str(int(dealer_id))
            dealer_payload = _build_dealer_payload(dealer_df)

            serialized_payload = json.dumps(
                dealer_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            pipe.set(
                dealer_key,
                serialized_payload,
                ex=ttl_seconds,
            )

            cached_dealers += 1
            cached_rows += len(dealer_df)

            if cached_dealers % batch_size == 0:
                await pipe.execute()

                logger.info(
                    "Cached dealer feature batch",
                    extra={
                        "cached_dealers": cached_dealers,
                        "cached_rows": cached_rows,
                    },
                )

        if cached_dealers % batch_size:
            await pipe.execute()

    logger.info(
        "Finished caching dealer features",
        extra={
            "cached_dealers": cached_dealers,
            "cached_rows": cached_rows,
            "duplicate_rows": duplicate_count,
        },
    )

    return cached_dealers
