from __future__ import annotations

from typing import Any


def _auction_id(item: dict[str, Any]) -> int | None:
    raw = item.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _sort_key(item: dict[str, Any]) -> int:

    aid = _auction_id(item)
    return aid if aid is not None else 0


def reorder_by_ids(
    items: list[dict[str, Any]],
    ordered_ids: list[int],
) -> list[dict[str, Any]]:

    by_id: dict[int, dict[str, Any]] = {}
    for it in items:
        aid = _auction_id(it)
        if aid is not None:
            by_id[aid] = it

    ranked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for aid in ordered_ids:
        if aid in by_id and aid not in seen:
            ranked.append(by_id[aid])
            seen.add(aid)

    remainder = [it for it in items if _auction_id(it) not in seen]
    remainder.sort(key=_sort_key)
    return ranked + remainder
