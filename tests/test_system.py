"""Runtime, migration and privacy checks across module boundaries."""

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import SCORE_FIELDS, Settings
from app.db import make_engine, utc_now
from app.errors import AppError
from app.main import create_app
from app.models import Feedback


def test_service_routes(client, settings):
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}
    assert client.get("/version").json() == {"version": settings.app_version}
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/v1/movies/{movie_id}/reviews" in schema["paths"]
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_health_database_failure_is_safe(client, app, monkeypatch):
    def fail():
        raise SQLAlchemyError("private-database-password")

    monkeypatch.setattr(app.state.engine, "connect", fail)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
    assert "private-database-password" not in response.text


def test_unexpected_errors_never_expose_secret(client, app, caplog):
    @app.get("/test-unexpected")
    def failing():
        raise RuntimeError("never-expose-this-secret")

    response = client.get("/test-unexpected")
    assert response.status_code == 500
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert "never-expose-this-secret" not in response.text + caplog.text


def test_not_found_has_uniform_error(client):
    response = client.get("/not-a-real-route")
    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_request_logs_omit_query_and_auth_headers(client, caplog):
    caplog.set_level(logging.INFO, logger="cinema")
    response = client.get(
        "/version?private_token=top-secret-query",
        headers={"Authorization": "Bearer top-secret-header"},
    )
    assert response.status_code == 200
    assert "request method=GET" in caplog.text
    assert "top-secret-query" not in caplog.text
    assert "top-secret-header" not in caplog.text


def test_log_level_environment_is_used(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    settings = Settings(
        _env_file=None, jwt_secret="test-setting-secret-with-at-least-32-characters"
    )
    assert settings.log_level == "WARNING"
    application = create_app(settings)
    try:
        assert logging.getLogger("cinema").level == logging.WARNING
    finally:
        application.state.engine.dispose()
        logging.getLogger("cinema").setLevel(logging.INFO)


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"idea": 1},
        dict.fromkeys(SCORE_FIELDS, 0),
        dict.fromkeys(SCORE_FIELDS, True),
        dict.fromkeys(SCORE_FIELDS, float("nan")),
        dict.fromkeys(SCORE_FIELDS, float("inf")),
    ],
)
def test_invalid_weight_configuration_is_rejected(invalid):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            jwt_secret="test-secret-more-than-thirty-two-characters",
            rating_weights=invalid,
        )


def test_invalid_configuration_has_safe_diagnostic(monkeypatch, caplog):
    from app.config import get_settings

    def invalid_settings():
        return Settings(_env_file=None, jwt_secret="sensitive")

    monkeypatch.setattr("app.main.get_settings", invalid_settings)
    with pytest.raises(RuntimeError) as exc:
        create_app()
    assert "sensitive" not in str(exc.value) + caplog.text
    get_settings.cache_clear()


def test_invalid_database_url_has_safe_diagnostic(settings, caplog):
    invalid = settings.model_copy(update={"database_url": "private-malformed-database-url"})
    with pytest.raises(RuntimeError) as exc:
        create_app(invalid)
    assert "private-malformed-database-url" not in str(exc.value) + caplog.text


def test_migrations_build_the_same_schema_as_models(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWT_SECRET", "migration-test-secret-with-at-least-32-characters")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = make_engine(database_url)
    try:
        assert set(inspect(engine).get_table_names()) >= {
            "users",
            "movies",
            "feedback",
            "invitations",
            "password_reset_tokens",
        }
    finally:
        engine.dispose()
    command.check(config)
    command.downgrade(config, "base")
    command.upgrade(config, "head")


def test_database_rejects_duplicate_votes_and_partial_scores(db, make_user, make_movie):
    user, movie = make_user(), make_movie()
    scores = dict.fromkeys(SCORE_FIELDS, 8)
    db.add(Feedback(user_id=user.id, movie_id=movie.id, **scores))
    db.commit()
    db.add(Feedback(user_id=user.id, movie_id=movie.id, **scores))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(Feedback(user_id=user.id, movie_id=movie.id, idea=8))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_saved_data_survives_a_new_app_instance(app, settings, make_user, make_movie, auth_headers):
    user, movie = make_user(), make_movie()
    headers = auth_headers(user)
    restarted = create_app(settings)
    with TestClient(restarted) as second_client:
        profile = second_client.get("/api/v1/users/me", headers=headers)
        assert profile.status_code == 200
        assert profile.json()["email"] == user.email
        detail = second_client.get(f"/api/v1/movies/{movie.id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["title"] == movie.title


def test_no_api_key_does_not_break_local_feedback(settings, make_user, make_movie, auth_headers):
    user, movie = make_user(), make_movie()
    headers = auth_headers(user)
    application = create_app(settings)
    with TestClient(application) as local_client:
        assert local_client.get("/api/v1/catalog/films", headers=headers).status_code == 503
        result = local_client.put(
            f"/api/v1/movies/{movie.id}/feedback/me",
            headers=headers,
            json={"scores": dict.fromkeys(SCORE_FIELDS, 9), "review": None},
        )
        assert result.status_code == 200
        assert result.json()["overall"] == 9


def test_stale_data_available_during_external_outage(client, app, db, make_movie, auth_headers):
    movie = make_movie(external_updated_at=utc_now() - timedelta(days=2))

    async def unavailable(_):
        raise AppError(503, "external_service_unavailable", "Unavailable")

    app.state.provider.get_movie = unavailable
    response = client.get(f"/api/v1/movies/{movie.id}", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["external_data_stale"] is True
    assert response.json()["title"] == movie.title
