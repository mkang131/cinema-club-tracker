"""Shared API fixtures never connect to real SMTP or movie services."""

from copy import deepcopy
from itertools import count

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, utc_now
from app.models import Movie, User
from app.security import create_access_token, hash_password
from app_final import create_app


class FakeProvider:
    configured = True

    def __init__(self):
        self.calls = []
        self.movie = {
            "kinopoisk_id": 301,
            "title": "Матрица",
            "original_name": "The Matrix",
            "year": 1999,
            "description": "Тестовое описание фильма",
            "genres": [{"id": 6, "name": "фантастика"}],
            "countries": [{"id": 1, "name": "США"}],
            "film_length": 136,
            "poster_url": "https://example.org/poster.jpg",
            "staff": [{"id": 1, "name": "Test Actor", "profession": "ACTOR", "role": "Neo"}],
            "external_ratings": {
                "kinopoisk": {"rating": 8.5, "votes": 100, "data_provider": "kinopoisk_unofficial"},
                "imdb": {"rating": None, "votes": None, "data_provider": "kinopoisk_unofficial"},
            },
            "external_updated_at": utc_now(),
            "metadata_incomplete": False,
        }

    async def get_movie(self, kinopoisk_id):
        self.calls.append(("get_movie", kinopoisk_id))
        movie = deepcopy(self.movie)
        movie["kinopoisk_id"] = kinopoisk_id
        movie["external_updated_at"] = utc_now()
        return movie

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return {
            "items": [deepcopy(self.movie)],
            "total": 1,
            "total_pages": 1,
            "page": 1,
            "page_size": 20,
        }

    async def filters(self):
        self.calls.append(("filters",))
        return {"genres": self.movie["genres"], "countries": self.movie["countries"]}

    async def aclose(self):
        pass


class RecordingMailer:
    def __init__(self):
        self.messages = []

    def send_reset(self, email, token):
        self.messages.append({"email": email, "token": token})


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        app_env="test",
        jwt_secret="test-only-secret-not-used-outside-tests-123456-abcdefghijklmnopqrstuv",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        kinopoisk_api_key=None,
        smtp_host=None,
    )


@pytest.fixture
def app(settings):
    application = create_app(settings, provider=FakeProvider(), mailer=RecordingMailer())
    Base.metadata.create_all(application.state.engine)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(app):
    with app.state.session_factory() as session:
        yield session


@pytest.fixture
def make_user(db):
    sequence = count(1)

    def factory(email=None, password="correct-horse-battery", **kwargs):
        if email is None:
            number = next(sequence)
            email = "member@example.org" if number == 1 else f"member{number}@example.org"
        user = User(email=email.strip().lower(), password_hash=hash_password(password), **kwargs)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return factory


@pytest.fixture
def auth_headers(settings, make_user):
    def factory(user=None):
        user = user or make_user()
        return {"Authorization": f"Bearer {create_access_token(user, settings)}"}

    return factory


@pytest.fixture
def make_movie(db):
    sequence = count(301)

    def factory(**overrides):
        data = deepcopy(FakeProvider().movie)
        data["kinopoisk_id"] = next(sequence)
        data.update(overrides)
        movie = Movie(**data)
        db.add(movie)
        db.commit()
        db.refresh(movie)
        return movie

    return factory
