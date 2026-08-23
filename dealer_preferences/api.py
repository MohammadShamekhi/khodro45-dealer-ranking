import logging

from fastapi import APIRouter, HTTPException, Query

from dealer_preferences.db import (
    fetch_dealer_preferences_for_dealer,
    fetch_latest_comment_extraction,
)
from dealer_preferences.transform import (
    build_preferences,
    load_dealer_preferences_from_df,
    serialize_extraction_row,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def get_dealer_preferences(
    dealer_id: int = Query(..., gt=0, description="Dealer user_ptr_id"),
):
    logger.info("GET /api/v1/dealer_preferences/ dealer_id=%d", dealer_id)
    try:
        comments_df = fetch_latest_comment_extraction(dealer_id)
        prefs_df = fetch_dealer_preferences_for_dealer(dealer_id)
    except Exception as exc:
        logger.exception("dealer_preferences database error dealer_id=%d", dealer_id)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc

    if comments_df.empty:
        logger.warning(
            "dealer_preferences not found dealer_id=%d reason=no_completed_extraction",
            dealer_id,
        )
        raise HTTPException(
            status_code=404,
            detail=f"No completed comment extraction found for dealer_id={dealer_id}",
        )

    dealer_preferences = load_dealer_preferences_from_df(prefs_df)
    preferences = build_preferences(
        comments_df,
        dealer_preferences=dealer_preferences,
        include_preferences_only=False,
    )

    logger.info(
        "GET /api/v1/dealer_preferences/ dealer_id=%d segment=%s",
        dealer_id,
        preferences[0].get("segment"),
    )
    return {
        "dealer_id": dealer_id,
        "extractions": serialize_extraction_row(comments_df.iloc[0]),
        "preferences": preferences[0],
    }
