"""Authenticated catalog, local films and private club feedback."""

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utc_now
from app.dependencies import get_current_user, get_db, get_settings
from app.errors import AppError
from app.models import Feedback, Movie, User
from app.ratings import (
    CATEGORIES,
    aggregate_feedback,
    rounded_statistics,
    weighted_overall,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Movies"], dependencies=[Depends(get_current_user)])
Database = Annotated[Session, Depends(get_db)]
Configuration = Annotated[Settings, Depends(get_settings)]
CurrentUser = Annotated[User, Depends(get_current_user)]
PositiveId = Annotated[int, Field(gt=0)]
Score = Annotated[int, Field(strict=True, ge=1, le=10)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class Scores(BaseModel):
    """All six original scores are mandatory; the server computes overall."""

    model_config = ConfigDict(extra="forbid")
    idea: Score
    plot: Score
    screenplay: Score
    acting: Score
    sound: Score
    visuals: Score


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scores: Scores | None
    review: Annotated[str, Field(strict=True, max_length=5000)] | None

    @field_validator("review", mode="before")
    @classmethod
    def trim_review(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def require_feedback(self) -> "FeedbackInput":
        if self.scores is None and self.review is None:
            raise ValueError("Provide scores or a non-empty review")
        return self


class ImportMovieInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kinopoisk_id: Annotated[int, Field(strict=True, gt=0)]


class Averages(BaseModel):
    idea: float | None
    plot: float | None
    screenplay: float | None
    acting: float | None
    sound: float | None
    visuals: float | None


class NamedValue(BaseModel):
    id: int | None = None
    name: str


class StaffMember(BaseModel):
    id: int | None = None
    name: str | None = None
    profession: str | None = None
    role: str | None = None


class ExternalRating(BaseModel):
    rating: float | None = None
    votes: int | None = None
    data_provider: str = "kinopoisk_unofficial"


class CatalogFilm(BaseModel):
    kinopoisk_id: int
    title: str
    original_name: str | None = None
    year: int | None = None
    poster_url: str | None = None
    genres: list[NamedValue] = Field(default_factory=list)
    countries: list[NamedValue] = Field(default_factory=list)
    external_ratings: dict[str, ExternalRating] = Field(default_factory=dict)


class CatalogPage(BaseModel):
    items: list[CatalogFilm]
    total: int = 0
    total_pages: int
    page: int
    page_size: int = 20


class CatalogFilters(BaseModel):
    genres: list[NamedValue]
    countries: list[NamedValue]


class MovieOutput(CatalogFilm):
    id: int
    description: str | None
    film_length: int | None
    staff: list[StaffMember]
    external_updated_at: datetime | None
    external_data_stale: bool
    metadata_incomplete: bool
    averages: Averages
    overall: float | None
    ratings_count: int
    reviews_count: int
    rating_weights: dict[str, float]


class MoviePage(BaseModel):
    items: list[MovieOutput]
    total: int
    page: int
    page_size: int


class FeedbackOutput(BaseModel):
    scores: Scores | None
    review: str | None
    overall: float | None
    created_at: datetime
    updated_at: datetime


class AnonymousReview(BaseModel):
    review_id: str
    text: str
    created_at: datetime


class ReviewsPage(BaseModel):
    items: list[AnonymousReview]
    total: int
    page: int
    page_size: int


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _not_found(entity: str) -> AppError:
    return AppError(404, f"{entity}_not_found", f"{entity.capitalize()} not found")


def _movie_or_404(db: Session, movie_id: int) -> Movie:
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise _not_found("movie")
    return movie


def _own_feedback(db: Session, user_id: int, movie_id: int) -> Feedback | None:
    return db.scalar(
        select(Feedback).where(Feedback.user_id == user_id, Feedback.movie_id == movie_id)
    )


def _statistics(db: Session, movie_id: int, settings: Settings) -> dict[str, Any]:
    records = db.scalars(select(Feedback).where(Feedback.movie_id == movie_id)).all()
    return aggregate_feedback(records, settings.rating_weights)


def _is_stale(movie: Movie, settings: Settings) -> bool:
    timestamp = _utc(movie.external_updated_at)
    if timestamp is None:
        return True
    return (_utc(utc_now()) - timestamp).total_seconds() >= settings.external_cache_ttl_seconds


def _movie_output(
    movie: Movie,
    statistics: dict[str, Any],
    settings: Settings,
    *,
    stale: bool | None = None,
) -> dict[str, Any]:
    return {
        "id": movie.id,
        "kinopoisk_id": movie.kinopoisk_id,
        "title": movie.title,
        "original_name": movie.original_name,
        "year": movie.year,
        "description": movie.description,
        "genres": movie.genres,
        "countries": movie.countries,
        "film_length": movie.film_length,
        "poster_url": movie.poster_url,
        "staff": movie.staff,
        "external_ratings": movie.external_ratings,
        "external_updated_at": _utc(movie.external_updated_at),
        "external_data_stale": _is_stale(movie, settings) if stale is None else stale,
        "metadata_incomplete": movie.metadata_incomplete,
        "rating_weights": settings.rating_weights,
        **rounded_statistics(statistics),
    }


def _feedback_output(feedback: Feedback, settings: Settings) -> dict[str, Any]:
    scores = (
        {key: getattr(feedback, key) for key in CATEGORIES} if feedback.idea is not None else None
    )
    return {
        "scores": scores,
        "review": feedback.review,
        "overall": (
            round(weighted_overall(scores, settings.rating_weights), 2)
            if scores is not None
            else None
        ),
        "created_at": _utc(feedback.created_at),
        "updated_at": _utc(feedback.updated_at),
    }


def _merge_nonempty(previous: dict, incoming: dict) -> dict:
    """Partial upstream payloads never erase known metadata or ratings."""
    result = dict(previous)
    for key, value in incoming.items():
        if isinstance(value, dict):
            result[key] = _merge_nonempty(result.get(key) or {}, value)
        elif value is not None and value != [] and value != "":
            result[key] = value
    return result


def _merge_named_values(previous: list[dict], incoming: list[dict]) -> list[dict]:
    """Keep known filter identifiers if an upstream dictionary lookup failed."""
    known = {item.get("name", "").casefold(): item for item in previous}
    return [
        _merge_nonempty(known.get(item.get("name", "").casefold(), {}), item) for item in incoming
    ]


def _apply_metadata(movie: Movie, data: dict[str, Any]) -> None:
    for key in (
        "title",
        "original_name",
        "year",
        "description",
        "genres",
        "countries",
        "film_length",
        "poster_url",
        "staff",
    ):
        value = data.get(key)
        if key == "title" and data.get("_title_missing") and movie.title:
            continue
        if key in ("genres", "countries") and value:
            value = _merge_named_values(getattr(movie, key) or [], value)
        if value is not None and value != [] and value != "":
            setattr(movie, key, value)
    movie.external_ratings = _merge_nonempty(
        movie.external_ratings or {}, data.get("external_ratings") or {}
    )
    movie.metadata_incomplete = data.get("metadata_incomplete", False)
    movie.external_updated_at = utc_now()


async def _refresh(request: Request, db: Session, movie: Movie, settings: Settings) -> bool:
    """Refresh old snapshots on reads; persisted club votes are never touched."""
    stale = _is_stale(movie, settings)
    if not stale:
        return False
    provider = request.app.state.provider
    if not provider.configured:
        return True
    try:
        data = await provider.get_movie(movie.kinopoisk_id)
    except AppError as exc:
        logger.warning("movie_refresh_failed movie_id=%s code=%s", movie.id, exc.code)
        return True
    _apply_metadata(movie, data)
    db.commit()
    db.refresh(movie)
    logger.info("movie_refreshed movie_id=%s", movie.id)
    return False


def _validate_range(low: int | float | None, high: int | float | None) -> None:
    if low is not None and high is not None and low > high:
        raise AppError(422, "validation_error", "Lower bound exceeds upper bound")


@router.get(
    "/catalog/films",
    response_model=CatalogPage,
    summary="Search the external film catalog",
    description="Only films (FILM). Provider supports at most 20 pages of 20 films.",
)
async def search_catalog(
    request: Request,
    query: Annotated[str | None, Query(max_length=250)] = None,
    genre_id: Annotated[int | None, Query(gt=0)] = None,
    country_id: Annotated[int | None, Query(gt=0)] = None,
    year_from: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    year_to: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    rating_from: Annotated[float | None, Query(ge=0, le=10)] = None,
    rating_to: Annotated[float | None, Query(ge=0, le=10)] = None,
    sort: Literal["rating_desc", "votes_desc", "year_desc"] = "rating_desc",
    page: Annotated[int, Query(ge=1, le=20)] = 1,
) -> dict:
    _validate_range(year_from, year_to)
    _validate_range(rating_from, rating_to)
    return await request.app.state.provider.search(
        query=query.strip() if query else None,
        genre_id=genre_id,
        country_id=country_id,
        year_from=year_from,
        year_to=year_to,
        rating_from=rating_from,
        rating_to=rating_to,
        sort=sort,
        page=page,
    )


@router.get(
    "/catalog/filters",
    response_model=CatalogFilters,
    summary="External genre and country dictionaries",
)
async def catalog_filters(request: Request) -> dict:
    return await request.app.state.provider.filters()


@router.post(
    "/movies", status_code=201, response_model=MovieOutput, summary="Import a film into the club"
)
async def import_movie(
    payload: ImportMovieInput,
    request: Request,
    response: Response,
    db: Database,
    settings: Configuration,
) -> dict:
    movie = db.scalar(select(Movie).where(Movie.kinopoisk_id == payload.kinopoisk_id))
    if movie is not None:
        response.status_code = 200
    else:
        data = await request.app.state.provider.get_movie(payload.kinopoisk_id)
        movie = Movie(kinopoisk_id=payload.kinopoisk_id)
        _apply_metadata(movie, data)
        db.add(movie)
        try:
            db.commit()
        except IntegrityError:
            # The unique index resolves concurrent imports of the same film.
            db.rollback()
            movie = db.scalar(select(Movie).where(Movie.kinopoisk_id == payload.kinopoisk_id))
            if movie is None:
                raise
            response.status_code = 200
        db.refresh(movie)
        logger.info("movie_imported movie_id=%s", movie.id)
    return _movie_output(movie, _statistics(db, movie.id, settings), settings)


@router.get(
    "/movies", response_model=MoviePage, summary="Browse, filter and sort the local club catalog"
)
def list_movies(
    db: Database,
    settings: Configuration,
    query: Annotated[str | None, Query(max_length=250)] = None,
    genre_id: Annotated[int | None, Query(gt=0)] = None,
    year_from: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    year_to: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    club_rating_from: Annotated[float | None, Query(ge=0, le=10)] = None,
    min_votes: Annotated[int, Query(ge=0)] = 0,
    sort: Literal["title_asc", "year_desc", "club_rating_desc"] = "title_asc",
    page: Page = 1,
    page_size: PageSize = 20,
) -> dict:
    _validate_range(year_from, year_to)
    # This small single-club application intentionally filters in Python: SQLite's
    # built-in lower()/NOCASE does not case-fold Cyrillic, and JSON stays portable.
    grouped: dict[int, list[Feedback]] = defaultdict(list)
    for feedback in db.scalars(select(Feedback)).all():
        grouped[feedback.movie_id].append(feedback)
    matched = []
    for movie in db.scalars(select(Movie)).all():
        if query and query.strip().casefold() not in movie.title.casefold():
            continue
        if genre_id is not None and not any(
            genre.get("id") == genre_id for genre in movie.genres or []
        ):
            continue
        if year_from is not None and (movie.year is None or movie.year < year_from):
            continue
        if year_to is not None and (movie.year is None or movie.year > year_to):
            continue
        stats = aggregate_feedback(grouped[movie.id], settings.rating_weights)
        if stats["ratings_count"] < min_votes:
            continue
        if club_rating_from is not None and (
            stats["overall"] is None or stats["overall"] < club_rating_from
        ):
            continue
        matched.append((movie, stats))
    if sort == "title_asc":
        matched.sort(key=lambda pair: (pair[0].title.casefold(), pair[0].id))
    elif sort == "year_desc":
        matched.sort(key=lambda pair: (-(pair[0].year or 0), pair[0].id))
    else:
        matched.sort(
            key=lambda pair: (
                pair[1]["overall"] is None,
                -(pair[1]["overall"] or 0),
                pair[0].id,
            )
        )
    offset = (page - 1) * page_size
    return {
        "items": [
            _movie_output(movie, stats, settings)
            for movie, stats in matched[offset : offset + page_size]
        ],
        "total": len(matched),
        "page": page,
        "page_size": page_size,
    }


@router.get(
    "/movies/{movie_id}",
    response_model=MovieOutput,
    summary="Film details and live club statistics",
)
async def movie_detail(
    movie_id: PositiveId,
    request: Request,
    db: Database,
    settings: Configuration,
) -> dict:
    movie = _movie_or_404(db, movie_id)
    stale = await _refresh(request, db, movie, settings)
    return _movie_output(movie, _statistics(db, movie_id, settings), settings, stale=stale)


@router.get(
    "/movies/{movie_id}/feedback/me",
    response_model=FeedbackOutput,
    summary="Read your own feedback",
)
def my_feedback(
    movie_id: PositiveId, db: Database, settings: Configuration, user: CurrentUser
) -> dict:
    _movie_or_404(db, movie_id)
    feedback = _own_feedback(db, user.id, movie_id)
    if feedback is None:
        raise _not_found("feedback")
    return _feedback_output(feedback, settings)


@router.put(
    "/movies/{movie_id}/feedback/me",
    response_model=FeedbackOutput,
    summary="Create or fully replace your feedback",
    description="Send both scores and review explicitly. null removes that part.",
)
def put_feedback(
    movie_id: PositiveId,
    payload: FeedbackInput,
    db: Database,
    settings: Configuration,
    user: CurrentUser,
) -> dict:
    _movie_or_404(db, movie_id)
    feedback = _own_feedback(db, user.id, movie_id)
    values = {
        key: getattr(payload.scores, key) if payload.scores is not None else None
        for key in CATEGORIES
    }
    values.update(review=payload.review, updated_at=utc_now())
    if feedback is None:
        feedback = Feedback(user_id=user.id, movie_id=movie_id, **values)
        db.add(feedback)
    else:
        for key, value in values.items():
            setattr(feedback, key, value)
    try:
        db.commit()
    except IntegrityError:
        # A simultaneous first PUT may win the unique user/movie constraint.
        db.rollback()
        feedback = _own_feedback(db, user.id, movie_id)
        if feedback is None:
            raise
        for key, value in values.items():
            setattr(feedback, key, value)
        db.commit()
    db.refresh(feedback)
    logger.info("feedback_saved movie_id=%s", movie_id)
    return _feedback_output(feedback, settings)


@router.delete("/movies/{movie_id}/feedback/me", status_code=204, summary="Delete your feedback")
def delete_feedback(movie_id: PositiveId, db: Database, user: CurrentUser) -> Response:
    _movie_or_404(db, movie_id)
    feedback = _own_feedback(db, user.id, movie_id)
    if feedback is None:
        raise _not_found("feedback")
    db.delete(feedback)
    db.commit()
    logger.info("feedback_deleted movie_id=%s", movie_id)
    return Response(status_code=204)


@router.get(
    "/movies/{movie_id}/reviews",
    response_model=ReviewsPage,
    summary="Paginated anonymous club reviews",
)
def reviews(movie_id: PositiveId, db: Database, page: Page = 1, page_size: PageSize = 20) -> dict:
    _movie_or_404(db, movie_id)
    entries = db.scalars(
        select(Feedback)
        .where(Feedback.movie_id == movie_id, Feedback.review.is_not(None))
        .order_by(Feedback.created_at, Feedback.id)
    ).all()
    offset = (page - 1) * page_size
    return {
        "items": [
            {
                "review_id": entry.id,
                "text": entry.review,
                "created_at": _utc(entry.created_at),
            }
            for entry in entries[offset : offset + page_size]
        ],
        "total": len(entries),
        "page": page,
        "page_size": page_size,
    }
