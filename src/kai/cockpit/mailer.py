"""Send magic-link emails via stdlib smtplib, or print to stdout."""

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from kai.cockpit.settings import get_cockpit_settings


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

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        if smtp_user and smtp_pass:
            has_tls = server.has_extn("starttls")
            if has_tls:
                server.starttls()
                server.ehlo()
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)
