"""Calculate ratings from original votes; round only at the API boundary."""

from collections.abc import Iterable, Mapping
from math import fsum
from typing import Any

CATEGORIES = ("idea", "plot", "screenplay", "acting", "sound", "visuals")


def _value(record: Any, key: str) -> Any:
    return record[key] if isinstance(record, Mapping) else getattr(record, key)


def weighted_overall(scores: Any, weights: Mapping[str, float]) -> float:
    """Normalize weights first so even very large finite weights cannot overflow."""
    scale = max(weights.values())
    normalized = {key: weights[key] / scale for key in CATEGORIES}
    return fsum(_value(scores, key) * normalized[key] for key in CATEGORIES) / fsum(
        normalized.values()
    )


def aggregate_feedback(records: Iterable[Any], weights: Mapping[str, float]) -> dict[str, Any]:
    """Return unrounded statistics for filtering, sorting and final presentation."""
    entries = list(records)
    votes = [entry for entry in entries if _value(entry, "idea") is not None]
    count = len(votes)
    return {
        "averages": {
            key: fsum(_value(vote, key) for vote in votes) / count if count else None
            for key in CATEGORIES
        },
        "overall": (
            fsum(weighted_overall(vote, weights) for vote in votes) / count if count else None
        ),
        "ratings_count": count,
        "reviews_count": sum(bool(_value(entry, "review")) for entry in entries),
    }


def rounded_statistics(statistics: Mapping[str, Any]) -> dict[str, Any]:
    """Keep JSON numbers while presenting at most two fractional digits."""
    return {
        **statistics,
        "averages": {
            key: round(value, 2) if value is not None else None
            for key, value in statistics["averages"].items()
        },
        "overall": (round(statistics["overall"], 2) if statistics["overall"] is not None else None),
    }
