import logging

import pandas as pd
import psycopg

from dealer_preferences.config import (
    COMMENT_EXTRACTIONS_TABLE,
    DEALERS_PREFERENCE_TABLE,
    get_k45_settings,
    get_postgres_settings,
)

logger = logging.getLogger(__name__)


def _fetch_df(settings: dict, query: str, params=None) -> pd.DataFrame:
    logger.debug("db query params=%s", params)
    with psycopg.connect(**settings) as conn:
        return pd.read_sql(query, conn, params=params)


def fetch_latest_comment_extraction(dealer_id: int) -> pd.DataFrame:
    query = f"""
        SELECT *
        FROM {COMMENT_EXTRACTIONS_TABLE}
        WHERE dealer_id = CAST(%s AS INTEGER)
   
          AND extraction_status = 'completed'
        ORDER BY updated_at DESC
        LIMIT 1
    """
    df = _fetch_df(get_postgres_settings(), query, (dealer_id,))
    logger.info(
        "fetch_latest_comment_extraction dealer_id=%d rows=%d",
        dealer_id,
        len(df),
    )
    return df


def fetch_dealer_preferences_for_dealer(dealer_id: int) -> pd.DataFrame:

    query = f"""
        SELECT
            id,
            dealer_id,
            model,
            year,
            price,
            enable_notif_by_preference,
            created_time
        FROM {DEALERS_PREFERENCE_TABLE}
        WHERE dealer_id = %s
        ORDER BY created_time DESC
        LIMIT 1
    """
    df = _fetch_df(get_k45_settings(), query, (dealer_id,))
    logger.info(
        "fetch_dealer_preferences_for_dealer dealer_id=%d rows=%d",
        dealer_id,
        len(df),
    )
    return df


def fetch_dealer_preferences() -> pd.DataFrame:

    query = f"""
        SELECT
            id,
            dealer_id,
            model,
            year,
            price,
            enable_notif_by_preference,
            created_time
        FROM {DEALERS_PREFERENCE_TABLE}
        ORDER BY dealer_id
    """
    df = _fetch_df(get_k45_settings(), query)
    logger.info("fetch_dealer_preferences rows=%d", len(df))
    return df
