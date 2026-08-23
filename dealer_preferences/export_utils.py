import json


def fmt_array(vals: list[int]) -> str:
    if not vals:
        return ""
    return "{" + ",".join(str(v) for v in sorted(set(vals))) + "}"


def preferences_to_rows(preferences: list[dict]) -> list[dict]:
    rows = []
    for pref in preferences:
        rows.append(
            {
                "dealer_id": pref["dealer_id"],
                "segment": pref["segment"],
                "model": fmt_array(pref["model"]),
                "year": "",
                "price": fmt_array(pref["price"]),
                "enable_notif_by_preference": pref["enable_notif_by_preference"],
                "model_titles": (
                    json.dumps(pref["model_titles"], ensure_ascii=False)
                    if pref["model_titles"]
                    else ""
                ),
                "updated_at": pref["updated_at"] or "",
            }
        )
    return rows


