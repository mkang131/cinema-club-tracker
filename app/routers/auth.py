"""Invitation-only registration, login and password recovery."""

import logging
from datetime import UTC, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utc_now
from app.dependencies import get_db, get_settings
from app.errors import AppError
from app.models import Invitation, PasswordResetToken, User
from app.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["Авторизация"])
logger = logging.getLogger(__name__)


def normalize_email(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


NormalizedEmail = Annotated[EmailStr, BeforeValidator(normalize_email)]
Password = Annotated[str, Field(strict=True, min_length=12, max_length=128, repr=False)]
Token = Annotated[str, Field(strict=True, min_length=20, max_length=256, repr=False)]
Db = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]


class AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterInput(AuthModel):
    email: NormalizedEmail
    password: Password
    invitation_code: Token


class LoginInput(AuthModel):
    email: NormalizedEmail
    password: Password


class ForgotPasswordInput(AuthModel):
    email: NormalizedEmail


class ResetPasswordInput(AuthModel):
    token: Token
    new_password: Password


class UserOutput(BaseModel):
    id: int
    email: str
    created_at: str


class TokenOutput(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MessageOutput(BaseModel):
    message: str


def _ip(request: Request) -> str:
    # X-Forwarded-For is untrusted; configure the ASGI server's trusted proxy list.
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, key: str, limit: int, window: int) -> None:
    if not request.app.state.limiter.check(key, limit, window):
        raise AppError(429, "rate_limit_exceeded", "Слишком много запросов. Повторите позже.")


@router.post("/register", status_code=201, response_model=UserOutput)
def register(payload: RegisterInput, db: Db) -> UserOutput:
    """Create a member using an unexpired invitation bound to their email."""
    email = str(payload.email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise AppError(409, "email_already_registered", "Этот email уже зарегистрирован.")

    password_hash = hash_password(payload.password)
    now = utc_now()
    # The conditional write claims the invitation under the database write lock.
    # If insertion fails, rollback also restores the invitation.
    invitation_id = db.scalar(
        update(Invitation)
        .where(
            Invitation.token_hash == hash_token(payload.invitation_code),
            Invitation.email == email,
            Invitation.used_at.is_(None),
            Invitation.expires_at > now,
        )
        .values(used_at=now)
        .returning(Invitation.id)
    )
    if invitation_id is None:
        db.rollback()
        raise AppError(422, "invalid_invitation", "Приглашение недействительно или истекло.")

    user = User(email=email, password_hash=password_hash)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "email_already_registered", "Этот email уже зарегистрирован.") from None
    db.refresh(user)
    logger.info("user_registered")
    return UserOutput(
        id=user.id,
        email=user.email,
        created_at=user.created_at.replace(tzinfo=UTC).isoformat(),
    )


@router.post("/login", response_model=TokenOutput)
def login(payload: LoginInput, request: Request, db: Db, settings: Config) -> TokenOutput:
    """Exchange JSON email/password credentials for a bearer access token."""
    _rate_limit(request, "login:" + _ip(request), 10, 600)
    user = db.scalar(select(User).where(User.email == str(payload.email)))
    valid = verify_password(
        payload.password, user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    )
    if user is None or not valid:
        raise AppError(401, "invalid_credentials", "Неверный email или пароль.")
    return TokenOutput(
        access_token=create_access_token(user, settings),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


def _send_password_reset(bind: Engine, email: str, mailer: Any) -> None:
    """Run lookup, token creation and email delivery after the HTTP response."""
    token = generate_token()
    try:
        with Session(bind) as db:
            user_id = db.scalar(select(User.id).where(User.email == email))
            if user_id is None:
                return
            # Obtain the per-database write lock before revoking previous tokens.
            # Concurrent requests therefore leave only the newest token valid.
            db.execute(
                update(User).where(User.id == user_id).values(auth_version=User.auth_version)
            )
            now = utc_now()
            db.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user_id,
                    PasswordResetToken.used_at.is_(None),
                )
                .values(used_at=now)
            )
            db.add(
                PasswordResetToken(
                    user_id=user_id,
                    token_hash=hash_token(token),
                    expires_at=now + timedelta(minutes=30),
                )
            )
            db.commit()
    except SQLAlchemyError:
        logger.error("password_reset_storage_failed")
        return
    try:
        mailer.send_reset(email, token)
    except Exception:
        # SMTP exceptions may contain recipient addresses and message content.
        logger.error("mail_delivery_failed")


@router.post("/forgot-password", status_code=202, response_model=MessageOutput)
def forgot_password(
    payload: ForgotPasswordInput,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Db,
) -> MessageOutput:
    """Always return a neutral response, including when email sending is suppressed."""
    _rate_limit(request, "forgot:" + _ip(request), 5, 3600)
    email = str(payload.email)
    if request.app.state.limiter.check("forgot-email:" + hash_token(email), 1, 60):
        background_tasks.add_task(
            _send_password_reset, db.get_bind(), email, request.app.state.mailer
        )
    return MessageOutput(message="Если аккаунт существует, инструкция отправлена на почту")


@router.post("/reset-password", status_code=204)
def reset_password(payload: ResetPasswordInput, db: Db) -> Response:
    """Atomically consume a recovery token and revoke every previous access token."""
    password_hash = hash_password(payload.new_password)
    now = utc_now()
    user_id = db.scalar(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == hash_token(payload.token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(PasswordResetToken.user_id)
    )
    if user_id is None:
        db.rollback()
        raise AppError(422, "invalid_reset_token", "Код восстановления недействителен или истёк.")
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(password_hash=password_hash, auth_version=User.auth_version + 1)
    )
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.commit()
    logger.info("password_reset_completed")
    return Response(status_code=204)
