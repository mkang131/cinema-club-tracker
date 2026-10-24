"""Provider tests use MockTransport exclusively; no Internet or API key is required."""

import asyncio
import logging
import time
from datetime import datetime

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.providers.kinopoisk import KinopoiskProvider

FILM = {
    "kinopoiskId": 42,
    "nameRu": "Учебный фильм",
    "nameOriginal": "Example film",
    "year": 2024,
    "description": "Условное описание фильма.",
    "filmLength": 110,
    "posterUrl": "https://example.org/poster.jpg",
    "genres": [{"genre": "драма"}],
    "countries": [{"country": "Франция"}],
    "ratingKinopoisk": 8.2,
    "ratingKinopoiskVoteCount": 200,
    "ratingImdb": 7.9,
    "ratingImdbVoteCount": 300,
}
FILTERS = {
    "genres": [{"id": 8, "genre": "драма"}],
    "countries": [{"id": 3, "country": "Франция"}],
}
STAFF = [
    {
        "staffId": 19,
        "nameRu": "Условный актёр",
        "nameEn": "Example actor",
        "professionKey": "ACTOR",
        "description": "Главный герой",
    },
    {"staffId": 20, "nameEn": "Example director", "professionKey": "DIRECTOR"},
]


def settings(api_key="test-provider-key"):
    return Settings(_env_file=None, jwt_secret="test-only-" + "s" * 48, kinopoisk_api_key=api_key)


def make_provider(handler, *, configured=True, rate_limit=False):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = KinopoiskProvider(settings() if configured else settings(None), client)
    if not rate_limit:
        provider.MIN_REQUEST_INTERVAL_SECONDS = 0
    return provider, client


@pytest.mark.asyncio
async def test_search_translates_parameters_and_normalizes_result():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [FILM], "total": 1000, "totalPages": 50})

    provider, client = make_provider(handle)
    async with client:
        result = await provider.search(
            query="  Учебный   ФИЛЬМ  ",
            genre_id=8,
            country_id=3,
            year_from=2000,
            year_to=2025,
            rating_from=7.5,
            rating_to=9.0,
            sort="votes_desc",
            page=2,
        )
    request = requests[0]
    assert request.url.path == "/api/v2.2/films"
    assert request.url.host == "kinopoiskapiunofficial.tech"
    assert request.headers["X-API-KEY"] == "test-provider-key"
    assert dict(request.url.params) == {
        "type": "FILM",
        "order": "NUM_VOTE",
        "page": "2",
        "keyword": "учебный фильм",
        "genres": "8",
        "countries": "3",
        "yearFrom": "2000",
        "yearTo": "2025",
        "ratingFrom": "7.5",
        "ratingTo": "9.0",
    }
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 5)
    assert result["total_pages"] == 20
    assert result["total"] == 1000
    assert result["page"] == 2
    assert result["page_size"] == 20
    assert result["items"][0]["kinopoisk_id"] == 42
    assert result["items"][0]["title"] == FILM["nameRu"]
    assert result["items"][0]["external_ratings"]["imdb"]["rating"] == 7.9
    assert "test-provider-key" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("sort,expected", [("rating_desc", "RATING"), ("year_desc", "YEAR")])
async def test_search_sort_mapping(sort, expected):
    def handle(request):
        assert request.url.params["order"] == expected
        return httpx.Response(200, json={"items": [], "total": 0, "totalPages": 0})

    provider, client = make_provider(handle)
    async with client:
        assert (await provider.search(sort=sort))["items"] == []


@pytest.mark.asyncio
async def test_search_cache_normalizes_queries_and_does_not_share_mutable_results():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [FILM], "total": 1, "totalPages": 1})

    provider, client = make_provider(handle)
    async with client:
        first = await provider.search(query=" Example  FILM ")
        first["items"][0]["title"] = "mutated"
        second = await provider.search(query="example film")
        second["items"].clear()
        third = await provider.search(query="example film")
        await provider.search(query="example film", page=2)
    assert third["items"][0]["title"] == FILM["nameRu"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_search_cache_makes_new_request():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [], "total": 0, "totalPages": 0})

    provider, client = make_provider(handle)
    provider.SEARCH_CACHE_TTL_SECONDS = 0
    async with client:
        await provider.search()
        await provider.search()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_filters_cache_and_field_mapping():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/api/v2.2/films/filters"
        return httpx.Response(200, json=FILTERS)

    provider, client = make_provider(handle)
    async with client:
        first = await provider.filters()
        first["genres"].clear()
        second = await provider.filters()
    assert second == {
        "genres": [{"id": 8, "name": "драма"}],
        "countries": [{"id": 3, "name": "Франция"}],
    }
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_movie_loads_staff_ratings_and_filter_identifiers():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/v2.2/films/42":
            return httpx.Response(200, json=FILM)
        if request.url.path == "/api/v1/staff":
            assert request.url.params["filmId"] == "42"
            return httpx.Response(200, json=STAFF)
        return httpx.Response(200, json=FILTERS)

    provider, client = make_provider(handle)
    async with client:
        result = await provider.get_movie(42)
        await provider.get_movie(42)
    assert result["kinopoisk_id"] == 42
    assert result["description"] == FILM["description"]
    assert result["film_length"] == 110
    assert result["genres"] == [{"id": 8, "name": "драма"}]
    assert result["staff"][0] == {
        "id": 19,
        "name": "Условный актёр",
        "profession": "ACTOR",
        "role": "Главный герой",
    }
    assert result["staff"][1]["profession"] == "DIRECTOR"
    assert result["external_ratings"]["kinopoisk"] == {
        "rating": 8.2,
        "votes": 200,
        "data_provider": "kinopoisk_unofficial",
    }
    assert result["metadata_incomplete"] is False
    assert isinstance(result["external_updated_at"], datetime)
    assert result["external_updated_at"].tzinfo is None
    assert len(calls) == 5  # Only the filter dictionary is cached by the adapter.


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_path", ["/api/v1/staff", "/api/v2.2/films/filters"])
async def test_movie_can_be_imported_when_additional_metadata_fails(failed_path):
    def handle(request):
        if request.url.path == failed_path:
            return httpx.Response(429, json={"message": "quota"})
        if request.url.path == "/api/v2.2/films/42":
            return httpx.Response(200, json=FILM)
        return httpx.Response(200, json=FILTERS if "filters" in request.url.path else STAFF)

    provider, client = make_provider(handle)
    async with client:
        result = await provider.get_movie(42)
    assert result["title"] == FILM["nameRu"]
    assert result["metadata_incomplete"] is True
    assert result["external_ratings"]["kinopoisk"]["rating"] == 8.2
    if failed_path.endswith("filters"):
        assert result["genres"] == [{"id": None, "name": "драма"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500, 503])
async def test_upstream_errors_are_safe_503_and_not_member_auth_errors(status, caplog):
    def handle(request):
        return httpx.Response(status, text="SECRET_PROVIDER_BODY test-provider-key")

    provider, client = make_provider(handle)
    with caplog.at_level(logging.WARNING), pytest.raises(AppError) as caught:
        async with client:
            await provider.get_movie(42)
    assert caught.value.status_code == 503
    assert caught.value.code == "external_service_unavailable"
    assert "SECRET_PROVIDER_BODY" not in str(caught.value)
    assert "test-provider-key" not in caplog.text
    assert "SECRET_PROVIDER_BODY" not in caplog.text


@pytest.mark.asyncio
async def test_missing_film_is_404():
    provider, client = make_provider(lambda request: httpx.Response(404))
    with pytest.raises(AppError) as caught:
        async with client:
            await provider.get_movie(42)
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_timeout_is_safe_503():
    def handle(request):
        raise httpx.ReadTimeout("secret-provider-key", request=request)

    provider, client = make_provider(handle)
    with pytest.raises(AppError) as caught:
        async with client:
            await provider.search()
    assert caught.value.status_code == 503
    assert "secret-provider-key" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "filters", "get_movie"])
async def test_missing_key_does_not_make_any_network_request(operation):
    def handle(request):
        pytest.fail("Unconfigured provider must never make HTTP requests")

    provider, client = make_provider(handle, configured=False)
    assert provider.configured is False
    with pytest.raises(AppError) as caught:
        async with client:
            if operation == "get_movie":
                await provider.get_movie(42)
            else:
                await getattr(provider, operation)()
    assert caught.value.code == "external_service_not_configured"


@pytest.mark.asyncio
async def test_absent_external_ratings_remain_null():
    def handle(request):
        return httpx.Response(
            200,
            json={
                "items": [{"kinopoiskId": 42, "nameEn": "Example"}],
                "total": 1,
                "totalPages": 1,
            },
        )

    provider, client = make_provider(handle)
    async with client:
        result = await provider.search()
    assert result["items"][0]["title"] == "Example"
    for rating in result["items"][0]["external_ratings"].values():
        assert rating["rating"] is None
        assert rating["votes"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], {"items": "invalid"}, {"items": [{}]}])
async def test_invalid_search_payload_is_503(body):
    provider, client = make_provider(lambda request: httpx.Response(200, json=body))
    with pytest.raises(AppError) as caught:
        async with client:
            await provider.search()
    assert caught.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slow_path,expected_partial",
    [
        ("/api/v2.2/films/42", False),
        ("/api/v1/staff", True),
    ],
)
async def test_movie_total_timeout_covers_all_upstream_calls(slow_path, expected_partial):
    async def handle(request):
        if request.url.path == slow_path:
            await asyncio.sleep(1)
        if request.url.path == "/api/v2.2/films/42":
            return httpx.Response(200, json=FILM)
        return httpx.Response(200, json=STAFF)

    provider, client = make_provider(handle)
    provider.MOVIE_TIMEOUT_SECONDS = 0.01
    async with client:
        if expected_partial:
            assert (await provider.get_movie(42))["metadata_incomplete"] is True
        else:
            with pytest.raises(AppError) as caught:
                await provider.get_movie(42)
            assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_rate_limiter_spaces_parallel_requests():
    started = []

    def handle(request):
        started.append(time.monotonic())
        return httpx.Response(200, json={"items": [], "total": 0, "totalPages": 0})

    provider, client = make_provider(handle, rate_limit=True)
    async with client:
        await asyncio.gather(*(provider.search(page=page) for page in (1, 2, 3)))
    assert len(started) == 3
    assert all(
        later - earlier >= 0.48 for earlier, later in zip(started, started[1:], strict=False)
    )
