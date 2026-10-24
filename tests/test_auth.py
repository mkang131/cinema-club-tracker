from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base, make_engine, utc_now
from app.errors import AppError
from app.mail import SMTPMailer
from app.models import Invitation, PasswordResetToken, User
from app.routers.auth import RegisterInput, ResetPasswordInput, register, reset_password
from app.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)

PREFIX = "/api/v1/auth"
PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "another-correct-password"


def invite(db, email="member@example.org", **changes):
    token = generate_token()
    values = {
        "email": email,
        "token_hash": hash_token(token),
        "expires_at": utc_now() + timedelta(days=7),
    }
    values.update(changes)
    db.add(Invitation(**values))
    db.commit()
    return token


def registration(code, email="member@example.org", password=PASSWORD):
    return {"email": email, "password": password, "invitation_code": code}


def login(client, email="member@example.org", password=PASSWORD):
    return client.post(PREFIX + "/login", json={"email": email, "password": password})


def test_register_login_and_private_profile(client, db):
    code = invite(db)
    response = client.post(PREFIX + "/register", json=registration(code, " MEMBER@EXAMPLE.ORG "))
    assert response.status_code == 201
    assert set(response.json()) == {"id", "email", "created_at"}
    assert response.json()["email"] == "member@example.org"
    assert response.json()["created_at"].endswith("+00:00")

    user = db.scalar(select(User))
    assert user.password_hash.startswith("$argon2id$")
    assert user.password_hash != PASSWORD
    assert verify_password(PASSWORD, user.password_hash)
    assert db.scalar(select(Invitation)).used_at is not None

    token_response = login(client, " MEMBER@EXAMPLE.ORG ")
    assert token_response.status_code == 200
    body = token_response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    profile = client.get(
        "/api/v1/users/me", headers={"Authorization": "Bearer " + body["access_token"]}
    )
    assert profile.status_code == 200
    assert profile.json()["email"] == "member@example.org"
    assert "password_hash" not in profile.json()
    assert "auth_version" not in profile.json()


@pytest.mark.parametrize("condition", ["expired", "used", "wrong_email", "unknown"])
def test_bad_invitation_is_rejected(client, db, condition):
    changes = {}
    if condition == "expired":
        changes["expires_at"] = utc_now() - timedelta(seconds=1)
    if condition == "used":
        changes["used_at"] = utc_now()
    code = invite(db, **changes)
    if condition == "unknown":
        code = generate_token()
    email = "someone@example.org" if condition == "wrong_email" else "member@example.org"
    response = client.post(PREFIX + "/register", json=registration(code, email))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_invitation"
    assert db.scalar(select(func.count(User.id))) == 0


def test_registered_email_conflicts_without_consuming_another_invitation(client, db, make_user):
    make_user()
    code = invite(db)
    response = client.post(PREFIX + "/register", json=registration(code))
    assert response.status_code == 409
    assert db.scalar(select(Invitation)).used_at is None


def test_invitation_can_only_be_used_once(client, db):
    code = invite(db)
    assert client.post(PREFIX + "/register", json=registration(code)).status_code == 201
    assert client.post(PREFIX + "/register", json=registration(code)).status_code == 409
    assert db.scalar(select(func.count(User.id))) == 1


@pytest.mark.parametrize("password", ["short", "p" * 129, 123456789123, True])
def test_invalid_password_rejected_without_consuming_invitation(client, db, password):
    code = invite(db)
    response = client.post(PREFIX + "/register", json=registration(code, password=password))
    assert response.status_code == 422
    assert db.scalar(select(Invitation)).used_at is None


def test_password_whitespace_and_case_are_preserved(client, db):
    password = "  CaseSensitive password  "
    code = invite(db)
    assert (
        client.post(PREFIX + "/register", json=registration(code, password=password)).status_code
        == 201
    )
    assert login(client, password=password).status_code == 200
    assert login(client, password=password.strip()).status_code == 401
    assert login(client, password=password.lower()).status_code == 401


def test_wrong_password_and_unknown_email_have_same_response(client, make_user):
    make_user()
    wrong_password = login(client, password="incorrect-password")
    unknown_email = login(client, email="unknown@example.org")
    assert wrong_password.status_code == unknown_email.status_code == 401
    first = wrong_password.json()["error"]
    second = unknown_email.json()["error"]
    assert first["code"] == second["code"] == "invalid_credentials"
    assert first["message"] == second["message"]


def test_login_rate_limit(client):
    for _ in range(10):
        assert login(client).status_code == 401
    assert login(client).status_code == 429


@pytest.mark.parametrize(
    "kind",
    ["expired", "tampered", "wrong_algorithm", "missing_exp", "invalid_sub", "huge_sub"],
)
def test_invalid_access_token_is_unauthorized(client, settings, make_user, kind):
    user = make_user()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "auth_version": 0,
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    algorithm = "HS256"
    if kind == "expired":
        payload["exp"] = now - timedelta(seconds=1)
    elif kind == "wrong_algorithm":
        algorithm = "HS384"
    elif kind == "missing_exp":
        payload.pop("exp")
    elif kind == "invalid_sub":
        payload["sub"] = "invalid"
    elif kind == "huge_sub":
        payload["sub"] = "9" * 5000
    token = jwt.encode(payload, settings.jwt_secret, algorithm=algorithm)
    if kind == "tampered":
        token = "x" + token
    response = client.get("/api/v1/users/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401


def test_protected_routes_require_token(client):
    for path in ("/api/v1/users/me", "/api/v1/movies", "/api/v1/catalog/films"):
        assert client.get(path).status_code == 401


def test_password_recovery_is_neutral_and_sends_email(client, app, db, make_user):
    make_user()
    known = client.post(PREFIX + "/forgot-password", json={"email": " MEMBER@EXAMPLE.ORG "})
    unknown = client.post(PREFIX + "/forgot-password", json={"email": "unknown@example.org"})
    assert known.status_code == unknown.status_code == 202
    assert (
        known.json()
        == unknown.json()
        == {"message": "Если аккаунт существует, инструкция отправлена на почту"}
    )
    messages = app.state.mailer.messages
    assert len(messages) == 1
    assert messages[0]["email"] == "member@example.org"
    token = messages[0]["token"]
    assert token not in known.text
    stored_token = db.scalar(select(PasswordResetToken))
    assert stored_token.token_hash == hash_token(token)
    assert stored_token.token_hash != token
    assert 29 * 60 < (stored_token.expires_at - utc_now()).total_seconds() <= 30 * 60


def test_recovery_cooldown_preserves_neutral_response(client, app, make_user):
    make_user()
    first = client.post(PREFIX + "/forgot-password", json={"email": "member@example.org"})
    second = client.post(PREFIX + "/forgot-password", json={"email": " MEMBER@EXAMPLE.ORG "})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(app.state.mailer.messages) == 1


def test_recovery_ip_rate_limit(client):
    for number in range(5):
        response = client.post(
            PREFIX + "/forgot-password", json={"email": f"unknown{number}@example.org"}
        )
        assert response.status_code == 202
    assert (
        client.post(PREFIX + "/forgot-password", json={"email": "unknown5@example.org"}).status_code
        == 429
    )


def test_reset_changes_password_invalidates_sessions_and_is_single_use(client, app, db, make_user):
    user = make_user()
    old_token = login(client).json()["access_token"]
    client.post(PREFIX + "/forgot-password", json={"email": user.email})
    token = app.state.mailer.messages[0]["token"]
    payload = {"token": token, "new_password": NEW_PASSWORD}
    assert client.post(PREFIX + "/reset-password", json=payload).status_code == 204
    assert client.post(PREFIX + "/reset-password", json=payload).status_code == 422
    assert login(client).status_code == 401
    assert login(client, password=NEW_PASSWORD).status_code == 200
    assert (
        client.get("/api/v1/users/me", headers={"Authorization": "Bearer " + old_token}).status_code
        == 401
    )
    db.refresh(user)
    assert user.auth_version == 1
    assert db.scalar(select(PasswordResetToken)).used_at is not None


def test_expired_reset_token_is_rejected(client, app, db, make_user):
    user = make_user()
    client.post(PREFIX + "/forgot-password", json={"email": user.email})
    token = app.state.mailer.messages[0]["token"]
    stored = db.scalar(select(PasswordResetToken))
    stored.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()
    assert (
        client.post(
            PREFIX + "/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
        ).status_code
        == 422
    )
    assert login(client).status_code == 200


def test_new_recovery_request_invalidates_previous_token(client, app, make_user):
    user = make_user()
    client.post(PREFIX + "/forgot-password", json={"email": user.email})
    first = app.state.mailer.messages[-1]["token"]
    # A fresh limiter simulates the end of the per-email cooldown.
    app.state.limiter = type(app.state.limiter)()
    client.post(PREFIX + "/forgot-password", json={"email": user.email})
    second = app.state.mailer.messages[-1]["token"]
    assert first != second
    assert (
        client.post(
            PREFIX + "/reset-password", json={"token": first, "new_password": NEW_PASSWORD}
        ).status_code
        == 422
    )
    assert (
        client.post(
            PREFIX + "/reset-password", json={"token": second, "new_password": NEW_PASSWORD}
        ).status_code
        == 204
    )


def test_mail_failure_is_logged_without_token_or_email(client, app, make_user, caplog):
    user = make_user()
    captured = {}

    class FailingMailer:
        def send_reset(self, email, token):
            captured.update(email=email, token=token)
            raise RuntimeError(f"delivery failure to {email}: {token}")

    app.state.mailer = FailingMailer()
    response = client.post(PREFIX + "/forgot-password", json={"email": user.email})
    assert response.status_code == 202
    assert "mail_delivery_failed" in caplog.text
    assert captured["email"] not in caplog.text
    assert captured["token"] not in caplog.text


def test_validation_and_auth_logs_do_not_expose_secrets(client, db, caplog):
    code = invite(db)
    secret = "private-password-never-logged"
    response = client.post(
        PREFIX + "/register",
        json={**registration(code, password=secret), "unexpected": "also-private"},
    )
    assert response.status_code == 422
    assert "also-private" not in response.text
    assert secret not in response.text
    assert code not in response.text
    assert secret not in caplog.text
    assert code not in caplog.text


def test_parallel_invitation_redemption_creates_one_user(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'concurrent-invite.db').as_posix()}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            code = invite(db)

        def redeem(_):
            with Session(engine) as db:
                try:
                    return register(RegisterInput(**registration(code)), db).id
                except AppError as error:
                    return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(redeem, range(2)))
        assert sum(type(result) is int for result in results) == 1
        with Session(engine) as db:
            assert db.scalar(select(func.count(User.id))) == 1
            assert db.scalar(select(Invitation)).used_at is not None
    finally:
        engine.dispose()


def test_parallel_token_redemption_changes_password_once(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'concurrent-reset.db').as_posix()}")
    Base.metadata.create_all(engine)
    token = generate_token()
    try:
        with Session(engine) as db:
            user = User(email="member@example.org", password_hash=hash_password(PASSWORD))
            db.add(user)
            db.flush()
            db.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=hash_token(token),
                    expires_at=utc_now() + timedelta(minutes=30),
                )
            )
            db.commit()

        def redeem(_):
            with Session(engine) as db:
                try:
                    return reset_password(
                        ResetPasswordInput(token=token, new_password=NEW_PASSWORD), db
                    ).status_code
                except AppError as error:
                    return error.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(redeem, range(2))) == [204, 422]
        with Session(engine) as db:
            user = db.scalar(select(User))
            assert user.auth_version == 1
            assert verify_password(NEW_PASSWORD, user.password_hash)
    finally:
        engine.dispose()


def test_revoked_user_version_cannot_access_profile(client, db, settings, make_user):
    user = make_user()
    token = create_access_token(user, settings)
    user.auth_version += 1
    db.commit()
    response = client.get("/api/v1/users/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401


def test_cli_creates_email_bound_invitation_and_prints_token_once(
    settings, db, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["create-invite", "--email", " MEMBER@EXAMPLE.ORG "]) == 0
    output = capsys.readouterr()
    token = output.out.strip().splitlines()[-1]
    assert len(token) >= 40
    assert output.out.count(token) == 1
    invitation = db.scalar(select(Invitation))
    assert invitation.email == "member@example.org"
    assert invitation.token_hash == hash_token(token)
    assert invitation.used_at is None
    assert timedelta(days=6, hours=23) < invitation.expires_at - utc_now() <= timedelta(days=7)


def test_cli_rejects_existing_member(settings, db, make_user, monkeypatch, capsys):
    make_user()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["create-invite", "--email", "member@example.org"]) == 1
    assert not capsys.readouterr().out
    assert db.scalar(select(func.count(Invitation.id))) == 0


def test_cli_database_error_is_actionable_without_details(settings, monkeypatch, capsys, tmp_path):
    configuration = settings.model_copy(
        update={"database_url": f"sqlite:///{(tmp_path / 'unmigrated.db').as_posix()}"}
    )
    monkeypatch.setattr(cli, "Settings", lambda: configuration)
    assert cli.main(["create-invite", "--email", "member@example.org"]) == 1
    output = capsys.readouterr()
    assert "alembic upgrade head" in output.err
    assert "member@example.org" not in output.err
    assert "Traceback" not in output.err
    assert not output.out


def test_smtp_mailer_uses_configured_transport_without_network(settings, monkeypatch):
    configuration = settings.model_copy(
        update={
            "smtp_host": "smtp.example.org",
            "smtp_port": 587,
            "smtp_tls": True,
            "smtp_user": "example-user",
            "smtp_password": "example-password",
            "smtp_from": "club@example.org",
        }
    )
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            captured["connection"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context):
            captured["tls"] = context

        def login(self, user, password):
            captured["credentials"] = (user, password)

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr("app.mail.smtplib.SMTP", FakeSMTP)
    token = generate_token()
    SMTPMailer(configuration).send_reset("member@example.org", token)
    assert captured["connection"] == ("smtp.example.org", 587, 5)
    assert captured["credentials"] == ("example-user", "example-password")
    assert captured["tls"] is not None
    message = captured["message"]
    assert message["From"] == "club@example.org"
    assert message["To"] == "member@example.org"
    assert token in message.get_content()
    assert "30 минут" in message.get_content()
