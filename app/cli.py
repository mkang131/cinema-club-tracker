"""Administrative commands intended to run on the club's own server."""

import argparse
import sys
from datetime import timedelta

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine, utc_now
from app.models import Invitation, User
from app.routers.auth import NormalizedEmail
from app.security import generate_token, hash_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Управление Cinema Club")
    commands = parser.add_subparsers(dest="command", required=True)
    create_invite = commands.add_parser("create-invite", help="Создать одноразовое приглашение")
    create_invite.add_argument("--email", required=True, help="Email будущего участника")
    create_invite.add_argument("--days", type=int, default=7, help="Срок действия в днях (1–365)")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 365:
        parser.error("--days должен быть от 1 до 365")

    try:
        email = str(TypeAdapter(NormalizedEmail).validate_python(args.email))
    except ValidationError:
        parser.error("Некорректный email")

    engine = None
    try:
        settings = Settings()
        engine = make_engine(settings.database_url)
        token = generate_token()
        with Session(engine) as db:
            if db.scalar(select(User.id).where(User.email == email)) is not None:
                print("Ошибка: этот email уже зарегистрирован.", file=sys.stderr)
                return 1
            db.add(
                Invitation(
                    email=email,
                    token_hash=hash_token(token),
                    expires_at=utc_now() + timedelta(days=args.days),
                )
            )
            db.commit()
        print("Одноразовый код приглашения (сохраните и передайте участнику):")
        print(token)
        return 0
    except (ValueError, OSError):
        print("Ошибка конфигурации. Проверьте переменные окружения.", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Не удалось создать приглашение. Проверьте DATABASE_URL и выполните alembic upgrade head.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
