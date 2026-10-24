"""SMTP transport. Reset tokens belong in the email only, never in logs."""

import smtplib
import ssl
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


class SMTPMailer:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings

    def send_reset(self, email: str, token: str) -> None:
        if not self.settings.smtp_host:
            raise RuntimeError("SMTP is not configured")

        message = EmailMessage()
        message["Subject"] = "Cinema Club: восстановление пароля"
        message["From"] = self.settings.smtp_from
        message["To"] = email
        message.set_content(
            "Вы запросили восстановление пароля в Cinema Club.\n\n"
            "Выполните POST /api/v1/auth/reset-password в Swagger /docs, "
            "передав этот код в поле token и новый пароль в new_password:\n\n"
            f"{token}\n\n"
            "Код действует 30 минут и может использоваться только один раз.\n"
            "Если вы не запрашивали восстановление, проигнорируйте это письмо.\n"
        )
        with smtplib.SMTP(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=5,
        ) as smtp:
            if self.settings.smtp_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.settings.smtp_user:
                smtp.login(self.settings.smtp_user, self.settings.smtp_password)
            smtp.send_message(message)
