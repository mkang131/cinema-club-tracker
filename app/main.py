"""Application factory; run with uvicorn app.main:create_app --factory."""

import logging
import time
import traceback
from contextlib import asynccontextmanager
from datetime import UTC
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException

from app.config import Settings, get_settings
from app.db import make_engine
from app.dependencies import get_current_user
from app.errors import AppError, ErrorResponse
from app.limiter import RateLimiter
from app.mail import SMTPMailer
from app.models import User
from app.providers.kinopoisk import KinopoiskProvider
from app.routers import auth, movies

logger = logging.getLogger("cinema")


def create_app(settings: Settings | None = None, *, provider=None, mailer=None) -> FastAPI:
    try:
        settings = settings or get_settings()
    except (ValidationError, SettingsError):
        logging.error("Invalid configuration; check variable names and values in .env.example")
        raise RuntimeError("Некорректные настройки. Проверьте .env.example и JWT_SECRET.") from None

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.setLevel(settings.log_level)
    # HTTP client DEBUG logs may include sensitive headers; never enable them here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    try:
        engine = make_engine(settings.database_url)
    except (ValueError, SQLAlchemyError, OSError):
        logger.error("Invalid database configuration")
        raise RuntimeError(
            "Не удалось настроить БД. Проверьте DATABASE_URL и права на папку."
        ) from None

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logger.info("startup version=%s", settings.app_version)
        if not settings.kinopoisk_api_key:
            logger.warning("external_service_not_configured")
        try:
            yield
        finally:
            await application.state.provider.aclose()
            engine.dispose()
            logger.info("shutdown")

    app = FastAPI(
        title="Cinema Club Tracker",
        description="Закрытый киноклуб: фильмы, шесть оценок, анонимные отзывы.",
        version=settings.app_version,
        lifespan=lifespan,
        responses={status: {"model": ErrorResponse} for status in (401, 404, 409, 422, 429, 503)},
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
    app.state.provider = provider if provider is not None else KinopoiskProvider(settings)
    app.state.mailer = mailer if mailer is not None else SMTPMailer(settings)
    app.state.limiter = RateLimiter()

    def error_response(request: Request, status: int, code: str, message: str, details=None):
        error = {"code": code, "message": message, "request_id": request.state.request_id}
        if details is not None:
            error["details"] = details
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse({"error": error}, status_code=status, headers=headers)

    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError):
        return error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic errors normally include submitted input, including credentials.
        details = [
            {"field": ".".join(map(str, item["loc"])), "type": item["type"]}
            for item in exc.errors()
        ]
        return error_response(
            request, 422, "validation_error", "Некорректные поля запроса", details
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(
            request, exc.status_code, "http_error", "Запрос не может быть выполнен"
        )

    async def unexpected_error(request: Request, exc: Exception):
        # Stack locations help diagnostics without serializing SQL parameters or request bodies.
        frames = [(frame.name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)]
        logger.error(
            "unexpected_error request_id=%s type=%s frames=%s",
            request.state.request_id,
            type(exc).__name__,
            frames,
        )
        return error_response(request, 500, "internal_error", "Внутренняя ошибка сервера")

    app.add_exception_handler(Exception, unexpected_error)

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            response = await unexpected_error(request, exc)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request method=%s path=%r status=%s duration_ms=%.2f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - started) * 1000,
            request.state.request_id,
        )
        return response

    @app.get("/health", tags=["Сервис"], summary="Проверить приложение и базу данных")
    def health():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            logger.error("database_unavailable")
            return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    @app.get("/version", tags=["Сервис"], summary="Версия приложения")
    def version():
        return {"version": settings.app_version}

    @app.get("/api/v1/users/me", tags=["Пользователь"], summary="Собственный профиль")
    def profile(user: User = Depends(get_current_user)):
        return {
            "id": user.id,
            "email": user.email,
            "created_at": user.created_at.replace(tzinfo=UTC),
        }

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(movies.router, prefix="/api/v1")
    return app
