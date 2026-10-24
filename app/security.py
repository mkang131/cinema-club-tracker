"""Password hashing and short-lived, revocable access tokens."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import User

_password_hash = PasswordHash.recommended()
# Verifying an unknown account takes the same expensive hashing path as a known one.
DUMMY_PASSWORD_HASH = _password_hash.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _password_hash.verify(password, stored_hash)
    except (UnknownHashError, ValueError):
        return False


def hash_token(token: str) -> str:
    """Hash a high-entropy invitation or reset token before persistence."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def create_access_token(user: "User", settings: "Settings") -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "auth_version": user.auth_version,
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, settings: "Settings") -> dict[str, Any]:
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        options={"require": ["sub", "exp", "iat", "auth_version"]},
    )
    subject = claims["sub"]
    auth_version = claims["auth_version"]
    if (
        not isinstance(subject, str)
        or len(subject) > 20
        or not subject.isascii()
        or not subject.isdecimal()
        or int(subject) <= 0
        or type(auth_version) is not int
        or auth_version < 0
    ):
        raise jwt.InvalidTokenError("Invalid access token claims")
    return claims
