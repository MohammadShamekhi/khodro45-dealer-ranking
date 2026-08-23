import ast
import json
import re

import pandas as pd

from dealer_preferences.config import MODEL_ID_TO_BRAND_MODEL, MODEL_TITLE_TO_IDS
from dealer_preferences.db import fetch_dealer_preferences


def parse_json_list(val) -> list:
    if isinstance(val, list):
        return val
    if pd.isna(val):
        return []
    s = str(val).strip()
    if s in ("[]", "", "nan", "{}"):
        return []
    try:
        return json.loads(s.replace('""', '"'))
    except Exception:
        try:
            return ast.literal_eval(s)
        except Exception:
            return []


def parse_int_list(val) -> list[int]:
    if isinstance(val, list):
        return sorted({int(v) for v in val})
    if pd.isna(val):
        return []
    s = str(val).strip()
    if s in ("", "{}", "[]", "nan"):
        return []
    if s.startswith("[") or s.startswith("{"):
        nums = re.findall(r"\d+", s)
        return sorted({int(n) for n in nums})
    return []


def derive_price(min_v, max_v) -> list[int]:
    min_v = None if pd.isna(min_v) else float(min_v)
    max_v = None if pd.isna(max_v) else float(max_v)

    if max_v == 500_000_000:
        return [10]
    if max_v == 1_000_000_000 and min_v == 500_000_000:
        return [20]
    if max_v == 2_000_000_000 and min_v == 1_000_000_000:
        return [30]
    if min_v == 2_000_000_000:
        return [40]
    return []


def load_dealer_preferences_from_df(prefs: pd.DataFrame) -> dict[int, dict]:
    by_dealer = {}
    for _, row in prefs.iterrows():
        by_dealer[int(row["dealer_id"])] = {
            "pref_model_ids": parse_int_list(row["model"]),
            "pref_price": parse_int_list(row["price"]),
            "enable_notif_by_preference": bool(row["enable_notif_by_preference"]),
            "pref_created_time": row.get("created_time"),
        }
    return by_dealer


def load_dealer_preferences() -> dict[int, dict]:
    return load_dealer_preferences_from_df(fetch_dealer_preferences())


def _index_comments(
    comments: pd.DataFrame, title_to_ids: dict[str, list[int]]
) -> dict[int, dict]:
    comments_by_dealer: dict[int, dict] = {}
    for _, row in comments.iterrows():
        dealer_id = int(row["dealer_id"])
        titles = parse_json_list(row["model"])
        model_ids: list[int] = []
        for title in titles:
            title = str(title).strip()
            if title in title_to_ids:
                model_ids.extend(title_to_ids[title])

        comments_by_dealer[dealer_id] = {
            "comment_model_titles": titles,
            "comment_model_ids": sorted(set(model_ids)),
            "comment_price": derive_price(row["budget_min"], row["budget_max"]),
            "updated_at": row.get("updated_at"),
        }
    return comments_by_dealer


def _brand_models(model_ids: list[int]) -> list[str]:
    if not model_ids:
        return []
    return [
        MODEL_ID_TO_BRAND_MODEL[model_id]
        for model_id in model_ids
        if model_id in MODEL_ID_TO_BRAND_MODEL
    ]


def _build_record(
    dealer_id: int,
    comment: dict | None,
    pref: dict | None,
    *,
    from_comments: bool,
) -> dict:
    in_comments = comment is not None
    in_prefs = pref is not None

    if in_comments and in_prefs:
        segment = "both"
    elif in_comments:
        segment = "comments_only"
    else:
        segment = "preferences_only"

    if from_comments:
        model_ids = comment["comment_model_ids"] if comment else []
        price = comment["comment_price"] if comment else []
        model_titles = comment["comment_model_titles"] if comment else []
        updated_at = comment["updated_at"] if comment else None
    else:
        model_ids = pref["pref_model_ids"] if pref else []
        price = pref["pref_price"] if pref else []
        model_titles = []
        updated_at = None

    if segment == "comments_only":
        enable_notif = False
    elif pref:
        enable_notif = pref["enable_notif_by_preference"]
    else:
        enable_notif = True

    return {
        "dealer_id": dealer_id,
        "segment": segment,
        "model": model_ids,
        "year": [],
        "price": price,
        "enable_notif_by_preference": enable_notif,
        "model_titles": model_titles,
        "brand_models": _brand_models(model_ids),
        "updated_at": _serialize_datetime(updated_at),
    }


def build_preferences(
    comments: pd.DataFrame,
    dealer_preferences: dict[int, dict] | None = None,
    title_to_ids: dict[str, list[int]] | None = None,
    *,
    include_preferences_only: bool = True,
) -> list[dict]:
    if dealer_preferences is None:
        dealer_preferences = load_dealer_preferences()
    if title_to_ids is None:
        title_to_ids = MODEL_TITLE_TO_IDS

    comments_by_dealer = _index_comments(comments, title_to_ids)
    comment_dealers = set(comments_by_dealer)
    pref_dealers = set(dealer_preferences)

    if include_preferences_only:
        all_dealers = sorted(comment_dealers | pref_dealers)
    else:
        all_dealers = sorted(comment_dealers)

    results = []
    for dealer_id in all_dealers:
        comment = comments_by_dealer.get(dealer_id)
        pref = dealer_preferences.get(dealer_id)
        from_comments = dealer_id in comment_dealers
        results.append(
            _build_record(dealer_id, comment, pref, from_comments=from_comments)
        )

    return results


def _serialize_datetime(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _serialize_cell(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value

    if hasattr(value, "tolist") and not hasattr(value, "isoformat"):
        return value.tolist()
    try:
        if pd.isna(value):
            return None
    except (ValueError, TypeError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def serialize_extraction_row(row) -> dict:

    return {key: _serialize_cell(value) for key, value in row.items()}
