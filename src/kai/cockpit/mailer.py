"""Send magic-link emails via stdlib smtplib, or print to stdout."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from kai.cockpit.settings import get_cockpit_settings

logger = logging.getLogger(__name__)


class MailError(RuntimeError):
    """Raised when an email cannot be sent."""


def send_magic_link(user_email: str, magic_url: str) -> None:
    """Send magic link email. Falls back to print if SMTP not configured."""
    settings = get_cockpit_settings()
    smtp_host = settings.smtp_host
    smtp_from = settings.smtp_from
    smtp_port = settings.smtp_port
    smtp_user = settings.smtp_user
    smtp_pass = settings.smtp_password

    if not smtp_host:
        print(f"Magic link for {user_email}: {magic_url}")
        return

    msg = EmailMessage()
    msg["Subject"] = "Cockpit Magic Link"
    msg["From"] = formataddr(("Knowledgeable AI", smtp_from))
    msg["To"] = user_email
    msg.set_content(
        f"Here's your one-time login link for the Cockpit:\n\n"
        f"{magic_url}\n\n"
        f"It expires in 10 minutes. If you didn't request it, you can "
        f"safely ignore this email — no one else can use this link."
    )

    try:
        with _smtp_server(smtp_host, smtp_port, smtp_user, smtp_pass) as server:
            server.send_message(msg)
    except MailError:
        raise
    except smtplib.SMTPException as exc:
        logger.exception("Failed to send magic link email to %s", user_email)
        raise MailError(f"Failed to send email: {exc}") from exc


def _smtp_server(host: str, port: int, user: str, password: str) -> smtplib.SMTP:
    if port == 465:
        server = smtplib.SMTP_SSL(host, port)
        try:
            server.ehlo()
            if user and password:
                server.login(user, password)
        except Exception:
            server.close()
            raise
        return server

    server = smtplib.SMTP(host, port)
    try:
        server.ehlo()
        if user and password:
            if not server.has_extn("starttls"):
                raise MailError(f"SMTP server {host}:{port} does not support STARTTLS")
            server.starttls()
            server.ehlo()
            server.login(user, password)
    except Exception:
        server.close()
        raise
    return server
