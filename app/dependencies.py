from collections.abc import Generator

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.models import User

bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Generator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    from app.security import decode_access_token

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError(401, "unauthorized", "Требуется вход в приложение")
    try:
        payload = decode_access_token(credentials.credentials, settings)
    except jwt.InvalidTokenError as exc:
        raise AppError(401, "unauthorized", "Недействительный токен") from exc
    try:
        subject = payload["sub"]
        if not isinstance(subject, str) or not subject.isdigit():
            raise ValueError
        user = db.get(User, int(subject))
    except (KeyError, ValueError, OverflowError) as exc:
        raise AppError(401, "unauthorized", "Недействительный токен") from exc
    version = payload.get("auth_version")
    if user is None or type(version) is not int or user.auth_version != version:
        raise AppError(401, "unauthorized", "Недействительный токен")
    return user
