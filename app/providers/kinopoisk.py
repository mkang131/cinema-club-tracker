"""Async Kinopoisk Unofficial adapter with safe errors and bounded memory caches."""

import asyncio
import logging
import math
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.errors import AppError

logger = logging.getLogger(__name__)


class KinopoiskProvider:
    BASE_URL = "https://kinopoiskapiunofficial.tech"
    REQUEST_TIMEOUT_SECONDS = 5
    MOVIE_TIMEOUT_SECONDS = 10
    MIN_REQUEST_INTERVAL_SECONDS = 0.5
    SEARCH_CACHE_TTL_SECONDS = 600
    FILTERS_CACHE_TTL_SECONDS = 86400
    MAX_CACHE_ENTRIES = 256
    SORTS = {"rating_desc": "RATING", "votes_desc": "NUM_VOTE", "year_desc": "YEAR"}

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self._api_key = settings.kinopoisk_api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
        self._rate_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @staticmethod
    def _unavailable() -> AppError:
        return AppError(
            503,
            "external_service_unavailable",
            "Внешний каталог временно недоступен. Повторите запрос позже.",
        )

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise AppError(
                503,
                "external_service_not_configured",
                "Внешний каталог не настроен.",
            )

    async def _wait_for_slot(self) -> None:
        async with self._rate_lock:
            if self._last_request_at is not None:
                delay = self.MIN_REQUEST_INTERVAL_SECONDS - (
                    time.monotonic() - self._last_request_at
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()

    async def _get(
        self, path: str, params: dict | None = None, *, movie_lookup: bool = False
    ) -> Any:
        self._ensure_configured()
        await self._wait_for_slot()
        try:
            response = await self._client.get(
                self.BASE_URL + path,
                params=params,
                headers={"X-API-KEY": self._api_key, "Accept": "application/json"},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            if movie_lookup and response.status_code == 404:
                raise AppError(404, "movie_not_found", "Фильм не найден во внешнем каталоге.")
            if response.status_code != 200:
                # Do not log provider bodies, request headers or exception messages.
                logger.warning("external_request_failed status=%s", response.status_code)
                raise self._unavailable()
            return response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("external_request_failed transport_or_payload")
            raise self._unavailable() from None

    def _cached(self, key: tuple) -> dict | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return deepcopy(value)

    def _remember(self, key: tuple, value: dict, ttl: int) -> dict:
        self._cache[key] = (time.monotonic() + ttl, deepcopy(value))
        self._cache.move_to_end(key)
        while len(self._cache) > self.MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return value

    @staticmethod
    def _text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        return value if type(value) is int and value >= 0 else None

    @staticmethod
    def _named(values: Any, raw_name: str) -> list[dict]:
        if not isinstance(values, list):
            return []
        return [
            {"id": KinopoiskProvider._integer(item.get("id")), "name": item[raw_name]}
            for item in values
            if isinstance(item, dict) and isinstance(item.get(raw_name), str)
        ]

    @staticmethod
    def _ratings(film: dict) -> dict:
        result = {}
        for source, key in (("kinopoisk", "Kinopoisk"), ("imdb", "Imdb")):
            rating = film.get("rating" + key)
            if (
                isinstance(rating, bool)
                or not isinstance(rating, (int, float))
                or not math.isfinite(rating)
                or not 0 <= rating <= 10
            ):
                rating = None
            votes = film.get("rating" + key + "VoteCount")
            if type(votes) is not int or votes < 0:
                votes = None
            result[source] = {
                "rating": rating,
                "votes": votes,
                "data_provider": "kinopoisk_unofficial",
            }
        return result

    def _summary(self, film: dict) -> dict:
        kinopoisk_id = film.get("kinopoiskId")
        if type(kinopoisk_id) is not int or kinopoisk_id <= 0:
            raise self._unavailable()
        return {
            "kinopoisk_id": kinopoisk_id,
            "title": self._text(film.get("nameRu"))
            or self._text(film.get("nameEn"))
            or self._text(film.get("nameOriginal"))
            or f"Фильм {kinopoisk_id}",
            "original_name": self._text(film.get("nameOriginal")) or self._text(film.get("nameEn")),
            "year": self._integer(film.get("year")),
            "poster_url": self._text(film.get("posterUrl"))
            or self._text(film.get("posterUrlPreview")),
            "genres": self._named(film.get("genres"), "genre"),
            "countries": self._named(film.get("countries"), "country"),
            "external_ratings": self._ratings(film),
        }

    async def search(
        self,
        *,
        query: str | None = None,
        genre_id: int | None = None,
        country_id: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        rating_from: float | None = None,
        rating_to: float | None = None,
        sort: str = "rating_desc",
        page: int = 1,
    ) -> dict:
        self._ensure_configured()
        if sort not in self.SORTS or not 1 <= page <= 20:
            raise AppError(422, "invalid_search", "Некорректная сортировка или страница поиска.")
        normalized_query = " ".join((query or "").split()).casefold()
        params = {"type": "FILM", "order": self.SORTS[sort], "page": page}
        for name, value in (
            ("keyword", normalized_query or None),
            ("genres", genre_id),
            ("countries", country_id),
            ("yearFrom", year_from),
            ("yearTo", year_to),
            ("ratingFrom", rating_from),
            ("ratingTo", rating_to),
        ):
            if value is not None:
                params[name] = value
        key = ("search", *sorted(params.items()))
        if (cached := self._cached(key)) is not None:
            return cached
        raw = await self._get("/api/v2.2/films", params)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise self._unavailable()
        try:
            result = {
                "items": [self._summary(item) for item in raw["items"]],
                "total": max(0, int(raw.get("total", 0))),
                "total_pages": max(0, min(20, int(raw.get("totalPages", 0)))),
                "page": page,
                "page_size": 20,
            }
        except (TypeError, ValueError, AttributeError, OverflowError):
            raise self._unavailable() from None
        return self._remember(key, result, self.SEARCH_CACHE_TTL_SECONDS)

    async def filters(self) -> dict:
        self._ensure_configured()
        key = ("filters",)
        if (cached := self._cached(key)) is not None:
            return cached
        raw = await self._get("/api/v2.2/films/filters")
        if not isinstance(raw, dict) or not all(
            isinstance(raw.get(name), list) for name in ("genres", "countries")
        ):
            raise self._unavailable()
        result = {
            "genres": self._named(raw["genres"], "genre"),
            "countries": self._named(raw["countries"], "country"),
        }
        return self._remember(key, result, self.FILTERS_CACHE_TTL_SECONDS)

    async def get_movie(self, kinopoisk_id: int) -> dict:
        self._ensure_configured()
        movie = None
        try:
            async with asyncio.timeout(self.MOVIE_TIMEOUT_SECONDS):
                raw = await self._get(f"/api/v2.2/films/{kinopoisk_id}", movie_lookup=True)
                if not isinstance(raw, dict) or raw.get("kinopoiskId") != kinopoisk_id:
                    raise self._unavailable()
                movie = {
                    **self._summary(raw),
                    "_title_missing": not any(
                        self._text(raw.get(field)) for field in ("nameRu", "nameEn", "nameOriginal")
                    ),
                    "description": self._text(raw.get("description")),
                    "film_length": self._integer(raw.get("filmLength")),
                    "staff": [],
                    "external_updated_at": datetime.now(UTC).replace(tzinfo=None),
                    "metadata_incomplete": False,
                }
                try:
                    staff = await self._get("/api/v1/staff", {"filmId": kinopoisk_id})
                    if not isinstance(staff, list):
                        raise self._unavailable()
                    movie["staff"] = [
                        {
                            "id": self._integer(person.get("staffId")),
                            "name": self._text(person.get("nameRu"))
                            or self._text(person.get("nameEn")),
                            "profession": self._text(person.get("professionKey")),
                            "role": self._text(person.get("description")),
                        }
                        for person in staff
                        if isinstance(person, dict)
                    ]
                    if len(movie["staff"]) != len(staff):
                        movie["metadata_incomplete"] = True
                except AppError:
                    movie["metadata_incomplete"] = True
                try:
                    filters = await self.filters()
                    for category in ("genres", "countries"):
                        ids = {item["name"].casefold(): item["id"] for item in filters[category]}
                        for item in movie[category]:
                            item["id"] = ids.get(item["name"].casefold())
                            if item["id"] is None:
                                movie["metadata_incomplete"] = True
                except AppError:
                    movie["metadata_incomplete"] = True
        except TimeoutError:
            logger.warning("external_movie_timeout")
            if movie is None:
                raise self._unavailable() from None
            movie["metadata_incomplete"] = True
        return movie

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
