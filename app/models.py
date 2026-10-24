"""Persistence models. Derived ratings are deliberately not stored."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utc_now


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    auth_version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(primary_key=True)
    kinopoisk_id: Mapped[int] = mapped_column(unique=True)
    title: Mapped[str] = mapped_column(String(1000))
    original_name: Mapped[str | None] = mapped_column(String(1000))
    year: Mapped[int | None]
    description: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list] = mapped_column(JSON, default=list)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    film_length: Mapped[int | None]
    poster_url: Mapped[str | None] = mapped_column(Text)
    staff: Mapped[list] = mapped_column(JSON, default=list)
    external_ratings: Mapped[dict] = mapped_column(JSON, default=dict)
    external_updated_at: Mapped[datetime | None]
    metadata_incomplete: Mapped[bool] = mapped_column(default=False)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "movie_id", name="uq_feedback_user_movie"),
        CheckConstraint(
            "(idea IS NULL AND plot IS NULL AND screenplay IS NULL AND acting IS NULL "
            "AND sound IS NULL AND visuals IS NULL) OR "
            "(idea IS NOT NULL AND plot IS NOT NULL AND screenplay IS NOT NULL "
            "AND acting IS NOT NULL AND sound IS NOT NULL AND visuals IS NOT NULL)",
            name="complete_scores",
        ),
        CheckConstraint("review IS NOT NULL OR idea IS NOT NULL", name="nonempty_feedback"),
        CheckConstraint(
            "review IS NULL OR length(trim(review)) BETWEEN 1 AND 5000", name="review_length"
        ),
        *(
            CheckConstraint(f"{field} IS NULL OR {field} BETWEEN 1 AND 10", name=f"{field}_range")
            for field in ("idea", "plot", "screenplay", "acting", "sound", "visuals")
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    idea: Mapped[int | None]
    plot: Mapped[int | None]
    screenplay: Mapped[int | None]
    acting: Mapped[int | None]
    sound: Mapped[int | None]
    visuals: Mapped[int | None]
    review: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)
