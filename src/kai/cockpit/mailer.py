"""Send magic-link emails via stdlib smtplib, or print to stdout."""

import os
from email.utils import formataddr


def send_magic_link(user_email: str, magic_url: str) -> None:
    """Send magic link email. Falls back to print if SMTP not configured."""
    smtp_host = os.environ.get("KAI_SMTP_HOST")
    if not smtp_host:
        print(f"Magic link for {user_email}: {magic_url}")
        return

    import smtplib
    from email.message import EmailMessage

    smtp_port = int(os.environ.get("KAI_SMTP_PORT", "587"))
    smtp_from = os.environ.get("KAI_SMTP_FROM", "kai@localhost")
    smtp_user = os.environ.get("KAI_SMTP_USER")
    smtp_pass = os.environ.get("KAI_SMTP_PASSWORD")

    msg = EmailMessage()
    msg["Subject"] = "Your kai cockpit login link"
    msg["From"] = formataddr(("Kai Cockpit", smtp_from))
    msg["To"] = user_email
    msg.set_content(f"Click to log in: {magic_url}\n\nThis link expires in 10 minutes.")

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        has_tls = server.has_extn("starttls")
        if smtp_user and smtp_pass and not has_tls:
            raise RuntimeError(
                "SMTP credentials configured but server does not support "
                "STARTTLS — refusing to send in cleartext"
            )
        if has_tls:
            server.starttls()
            server.ehlo()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)
