from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import utc_now
from app.errors import AppError
from app.models import Feedback, Movie
from app.ratings import CATEGORIES


class MovieProvider:
    configured = True

    def __init__(self):
        self.calls = []
        self.movie_error = None
        self.data = {
            "title": "Интерстеллар",
            "original_name": "Interstellar",
            "year": 2014,
            "description": "Space exploration",
            "genres": [{"id": 1, "name": "фантастика"}],
            "countries": [{"id": 1, "name": "США"}],
            "film_length": 169,
            "poster_url": "https://example.com/poster.jpg",
            "staff": [{"id": 10, "name": "Director", "profession": "DIRECTOR", "role": None}],
            "external_ratings": {
                "kinopoisk": {"rating": 8.6, "votes": 100},
                "imdb": {"rating": None, "votes": None},
            },
            "metadata_incomplete": False,
        }

    async def get_movie(self, kinopoisk_id):
        self.calls.append(("movie", kinopoisk_id))
        if self.movie_error:
            raise self.movie_error
        return {"kinopoisk_id": kinopoisk_id, **deepcopy(self.data)}

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return {
            "items": [{"kinopoisk_id": 258687, "title": "Interstellar"}],
            "page": kwargs["page"],
            "total_pages": 1,
        }

    async def filters(self):
        self.calls.append(("filters",))
        return {"genres": [{"id": 1, "name": "фантастика"}], "countries": []}

    async def aclose(self):
        pass


@pytest.fixture
def provider(app):
    provider = MovieProvider()
    app.state.provider = provider
    return provider


def scores(value=8, **overrides):
    return {**dict.fromkeys(CATEGORIES, value), **overrides}


def submit(client, headers, movie_id, score_values=None, review=None):
    return client.put(
        f"/api/v1/movies/{movie_id}/feedback/me",
        headers=headers,
        json={"scores": score_values, "review": review},
    )


def test_catalog_is_closed(client):
    for path in ("/api/v1/movies", "/api/v1/catalog/films", "/api/v1/catalog/filters"):
        assert client.get(path).status_code == 401


def test_import_is_persistent_and_idempotent(client, make_user, auth_headers, provider, db):
    headers = auth_headers(make_user())
    first = client.post("/api/v1/movies", json={"kinopoisk_id": 258687}, headers=headers)
    assert first.status_code == 201
    assert first.json()["title"] == "Интерстеллар"
    assert first.json()["overall"] is None
    second = client.post("/api/v1/movies", json={"kinopoisk_id": 258687}, headers=headers)
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert provider.calls == [("movie", 258687)]
    assert db.scalar(select(func.count()).select_from(Movie)) == 1
    db.expire_all()
    assert db.get(Movie, first.json()["id"]).title == "Интерстеллар"


def test_external_search_maps_parameters_and_validates_ranges(
    client, make_user, auth_headers, provider
):
    headers = auth_headers(make_user())
    response = client.get(
        "/api/v1/catalog/films",
        headers=headers,
        params={
            "query": " Space ",
            "genre_id": 1,
            "country_id": 2,
            "year_from": 2000,
            "year_to": 2020,
            "rating_from": 7,
            "rating_to": 10,
            "sort": "year_desc",
            "page": 2,
        },
    )
    assert response.status_code == 200
    assert provider.calls[-1][1] == {
        "query": "Space",
        "genre_id": 1,
        "country_id": 2,
        "year_from": 2000,
        "year_to": 2020,
        "rating_from": 7.0,
        "rating_to": 10.0,
        "sort": "year_desc",
        "page": 2,
    }
    assert client.get("/api/v1/catalog/filters", headers=headers).json()["genres"]
    for params in (
        {"year_from": 2020, "year_to": 2000},
        {"rating_from": 9, "rating_to": 2},
        {"rating_from": -1},
        {"page": 21},
        {"sort": "unsupported"},
    ):
        assert (
            client.get("/api/v1/catalog/films", headers=headers, params=params).status_code == 422
        )


def test_local_search_cyrillic_genres_years_sorting_and_pages(
    client, make_user, auth_headers, make_movie
):
    headers = auth_headers(make_user())
    first = make_movie(title="Звёздный путь", year=2009, genres=[{"id": 1, "name": "фантастика"}])
    make_movie(title="ЗВЁЗДНЫЙ десант", year=1997, genres=[{"id": 1, "name": "фантастика"}])
    make_movie(title="Комедия", year=2020, genres=[{"id": 2, "name": "комедия"}])
    result = client.get(
        "/api/v1/movies",
        headers=headers,
        params={"query": "зВЁЗДНЫЙ", "genre_id": 1, "sort": "year_desc", "page_size": 1},
    ).json()
    assert result["total"] == 2
    assert result["items"][0]["id"] == first.id
    filtered = client.get(
        "/api/v1/movies",
        headers=headers,
        params={"genre_id": 1, "year_from": 2000, "year_to": 2010},
    ).json()
    assert [item["id"] for item in filtered["items"]] == [first.id]
    assert client.get("/api/v1/movies", headers=headers, params={"page": 100}).json()["items"] == []
    assert (
        client.get(
            "/api/v1/movies", headers=headers, params={"year_from": 2020, "year_to": 2000}
        ).status_code
        == 422
    )


def test_votes_reviews_updates_and_deletion(client, make_user, auth_headers, make_movie, provider):
    movie = make_movie(external_updated_at=utc_now())
    a, b = auth_headers(make_user()), auth_headers(make_user())
    path = f"/api/v1/movies/{movie.id}"
    first = submit(client, a, movie.id, scores(7))
    assert first.status_code == 200
    assert first.json()["overall"] == 7
    assert submit(client, a, movie.id, scores(7)).status_code == 200
    assert submit(client, b, movie.id, scores(10), "  Great movie  ").status_code == 200
    result = client.get(path, headers=a).json()
    assert result["averages"] == dict.fromkeys(CATEGORIES, 8.5)
    assert result["overall"] == 8.5
    assert result["ratings_count"] == 2
    assert result["reviews_count"] == 1
    assert client.delete(f"{path}/feedback/me", headers=a).status_code == 204
    result = client.get(path, headers=a).json()
    assert result["overall"] == 10
    assert result["ratings_count"] == 1
    assert submit(client, b, movie.id, None, "Keep review").status_code == 200
    result = client.get(path, headers=a).json()
    assert result["overall"] is None
    assert result["averages"] == dict.fromkeys(CATEGORIES, None)
    assert result["ratings_count"] == 0
    assert result["reviews_count"] == 1
    assert provider.calls == []


def test_weight_changes_recalculate_and_unrounded_values_drive_filters(
    client, make_user, auth_headers, make_movie, settings
):
    movie = make_movie(external_updated_at=utc_now())
    headers = auth_headers(make_user())
    submit(client, headers, movie.id, scores(6, visuals=7))
    submit(client, auth_headers(make_user()), movie.id, scores(10))
    path = f"/api/v1/movies/{movie.id}"
    assert client.get(path, headers=headers).json()["overall"] == 8.08
    # Actual mean is 8.083333, even though its JSON representation is 8.08.
    assert (
        client.get("/api/v1/movies", headers=headers, params={"club_rating_from": 8.081}).json()[
            "total"
        ]
        == 1
    )
    settings.rating_weights = {**dict.fromkeys(CATEGORIES, 1), "visuals": 5}
    result = client.get(path, headers=headers).json()
    assert result["overall"] == 8.25
    assert result["ratings_count"] == 2
    assert result["rating_weights"]["visuals"] == 5


@pytest.mark.parametrize("invalid", [0, 11, 1.5, True, "8", None])
def test_score_types_and_range_are_strict(invalid, client, make_user, auth_headers, make_movie):
    movie = make_movie()
    assert (
        submit(client, auth_headers(make_user()), movie.id, scores(8, idea=invalid)).status_code
        == 422
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": {"idea": 8}, "review": None},
        {"scores": scores(), "review": None, "overall": 10},
        {"scores": {**scores(), "overall": 10}, "review": None},
        {"scores": scores(), "review": None, "rating_weights": {}},
        {"scores": scores(), "review": None, "user_id": "somebody"},
        {"scores": None, "review": None},
        {"scores": None, "review": "   "},
        {"scores": scores()},
        {"review": "text"},
        {"scores": None, "review": "a" * 5001},
    ],
)
def test_feedback_contract_rejects_incomplete_or_derived_data(
    payload, client, make_user, auth_headers, make_movie
):
    movie = make_movie()
    response = client.put(
        f"/api/v1/movies/{movie.id}/feedback/me",
        headers=auth_headers(make_user()),
        json=payload,
    )
    assert response.status_code == 422


def test_own_feedback_is_private_and_reviews_are_anonymous_and_paginated(
    client, make_user, auth_headers, make_movie
):
    movie = make_movie()
    a, b, c = [auth_headers(make_user()) for _ in range(3)]
    path = f"/api/v1/movies/{movie.id}"
    assert submit(client, a, movie.id, review="<b>Plain text</b>").status_code == 200
    assert client.get(f"{path}/feedback/me", headers=b).status_code == 404
    assert client.delete(f"{path}/feedback/me", headers=b).status_code == 404
    assert submit(client, b, movie.id, review="Second").status_code == 200
    assert submit(client, c, movie.id, scores(9)).status_code == 200
    collected = []
    for page in (1, 2):
        result = client.get(
            f"{path}/reviews", headers=c, params={"page": page, "page_size": 1}
        ).json()
        assert result["total"] == 2
        collected.extend(result["items"])
    assert {review["text"] for review in collected} == {"<b>Plain text</b>", "Second"}
    assert len({review["review_id"] for review in collected}) == 2
    for review in collected:
        assert set(review) == {"review_id", "text", "created_at"}
        assert review["created_at"].endswith("Z") or review["created_at"].endswith("+00:00")
    assert client.get(f"{path}/feedback/me", headers=a).json()["review"] == "<b>Plain text</b>"
    assert client.get(f"{path}/reviews", headers=c, params={"page": 3}).json()["items"] == []


def test_club_sorting_uses_votes_and_put_does_not_duplicate(
    client, make_user, auth_headers, make_movie, db
):
    headers = auth_headers(make_user())
    first, second, empty = make_movie(), make_movie(), make_movie()
    submit(client, headers, first.id, scores(7))
    submit(client, headers, first.id, scores(7))
    submit(client, headers, second.id, scores(9))
    result = client.get(
        "/api/v1/movies", headers=headers, params={"sort": "club_rating_desc"}
    ).json()
    assert [item["id"] for item in result["items"]] == [second.id, first.id, empty.id]
    assert (
        client.get("/api/v1/movies", headers=headers, params={"min_votes": 1}).json()["total"] == 2
    )
    assert db.scalar(select(func.count()).select_from(Feedback)) == 2


def test_stale_refresh_preserves_feedback_and_partial_metadata(
    client, make_user, auth_headers, make_movie, provider
):
    movie = make_movie(
        title="Known title",
        external_updated_at=utc_now() - timedelta(days=2),
        description="Known description",
        genres=[{"id": 1, "name": "фантастика"}],
        countries=[{"id": 1, "name": "США"}],
        staff=[{"id": 99, "name": "Known director", "profession": "DIRECTOR", "role": None}],
        external_ratings={"kinopoisk": {"rating": 9.0, "votes": 100}},
    )
    headers = auth_headers(make_user())
    submit(client, headers, movie.id, scores(9), "Club review")
    assert provider.calls == []
    provider.data.update(
        title=f"Фильм {movie.kinopoisk_id}",
        _title_missing=True,
        description=None,
        staff=[],
        metadata_incomplete=True,
    )
    provider.data["genres"][0]["id"] = None
    provider.data["countries"][0]["id"] = None
    provider.data["external_ratings"]["kinopoisk"] = {"rating": None, "votes": None}
    result = client.get(f"/api/v1/movies/{movie.id}", headers=headers).json()
    assert result["external_data_stale"] is False
    assert result["metadata_incomplete"] is True
    assert result["title"] == "Known title"
    assert result["description"] == "Known description"
    assert result["staff"][0]["id"] == 99
    assert result["external_ratings"]["kinopoisk"]["rating"] == 9.0
    assert result["genres"][0]["id"] == 1
    assert result["countries"][0]["id"] == 1
    assert result["overall"] == 9
    client.get(f"/api/v1/movies/{movie.id}", headers=headers)
    assert provider.calls == [("movie", movie.kinopoisk_id)]


@pytest.mark.parametrize(
    "code", ["external_service_unavailable", "external_service_not_configured"]
)
def test_upstream_failures_keep_cached_movies_and_local_writes(
    code, client, make_user, auth_headers, make_movie, provider
):
    movie = make_movie(external_updated_at=utc_now() - timedelta(days=2))
    headers = auth_headers(make_user())
    provider.movie_error = AppError(503, code, "External catalog unavailable")
    result = client.get(f"/api/v1/movies/{movie.id}", headers=headers)
    assert result.status_code == 200
    assert result.json()["external_data_stale"] is True
    assert submit(client, headers, movie.id, scores(8), "Still works").status_code == 200
    failed_import = client.post("/api/v1/movies", headers=headers, json={"kinopoisk_id": 999999})
    assert failed_import.status_code == 503
    assert failed_import.json()["error"]["code"] == code


def test_unknown_movie_and_no_own_record(client, make_user, auth_headers, make_movie):
    headers = auth_headers(make_user())
    for suffix in ("", "/reviews", "/feedback/me"):
        assert client.get(f"/api/v1/movies/999999{suffix}", headers=headers).status_code == 404
    movie = make_movie()
    assert client.get(f"/api/v1/movies/{movie.id}/reviews", headers=headers).json()["total"] == 0
    assert client.get(f"/api/v1/movies/{movie.id}/feedback/me", headers=headers).status_code == 404


def test_database_unique_and_score_constraints(db, make_user, make_movie):
    movie, user = make_movie(), make_user()
    db.add(Movie(kinopoisk_id=movie.kinopoisk_id, title="Duplicate"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(Feedback(user_id=user.id, movie_id=movie.id, **scores(11)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(Feedback(user_id=user.id, movie_id=movie.id, **scores(8)))
    db.commit()
    db.add(Feedback(user_id=user.id, movie_id=movie.id, **scores(9)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
