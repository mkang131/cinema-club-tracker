from types import SimpleNamespace

import pytest

from app.ratings import (
    CATEGORIES,
    aggregate_feedback,
    rounded_statistics,
    weighted_overall,
)


def vote(value, **overrides):
    return SimpleNamespace(**{**dict.fromkeys(CATEGORIES, value), "review": None, **overrides})


def test_equal_weights_and_separate_six_categories():
    scores = dict(zip(CATEGORIES, [9, 8, 9, 8, 10, 9], strict=True))
    assert weighted_overall(scores, dict.fromkeys(CATEGORIES, 1)) == pytest.approx(53 / 6)


def test_nonuniform_weights_apply_to_original_scores():
    weights = {**dict.fromkeys(CATEGORIES, 1), "visuals": 5}
    assert weighted_overall(vote(1, visuals=10), weights) == 5.5


def test_large_finite_weights_do_not_overflow():
    assert weighted_overall(vote(10), dict.fromkeys(CATEGORIES, 1e308)) == 10


def test_aggregation_counts_scores_and_reviews_separately():
    stats = aggregate_feedback(
        [vote(7), vote(10, review="Great"), vote(None, review="Text only")],
        dict.fromkeys(CATEGORIES, 1),
    )
    assert stats["ratings_count"] == 2
    assert stats["reviews_count"] == 2
    assert stats["averages"] == dict.fromkeys(CATEGORIES, 8.5)
    assert stats["overall"] == 8.5


def test_club_average_never_uses_rounded_individual_overalls():
    stats = aggregate_feedback([vote(6, visuals=7), vote(10)], dict.fromkeys(CATEGORIES, 1))
    assert stats["overall"] == pytest.approx(97 / 12)
    assert rounded_statistics(stats)["overall"] == 8.08


def test_no_votes_means_null_and_zero_counts():
    stats = aggregate_feedback([], dict.fromkeys(CATEGORIES, 1))
    assert stats == {
        "averages": dict.fromkeys(CATEGORIES, None),
        "overall": None,
        "ratings_count": 0,
        "reviews_count": 0,
    }
