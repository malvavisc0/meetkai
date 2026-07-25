"""Tests for Cockpit magic-link email delivery."""

from email.message import EmailMessage

from kai.cockpit import mailer


class _SmtpServer:
    def __init__(self) -> None:
        self.message: EmailMessage | None = None

    def __enter__(self) -> "_SmtpServer":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def send_message(self, message: EmailMessage) -> None:
        self.message = message


def test_send_magic_link_includes_text_and_html_alternatives(monkeypatch) -> None:
    server = _SmtpServer()
    magic_url = "https://cockpit.example.test/login?token=example-token"

    monkeypatch.setattr(mailer, "_smtp_server", lambda *_: server)

    mailer.send_magic_link("operator@example.test", magic_url)

    assert server.message is not None
    assert server.message["To"] == "operator@example.test"
    assert server.message.get_content_type() == "multipart/alternative"
    plain_part, html_part = server.message.iter_parts()
    assert plain_part.get_content_type() == "text/plain"
    assert magic_url in plain_part.get_content()
    assert html_part.get_content_type() == "text/html"
    html_content = html_part.get_content()
    assert f'href="{magic_url}"' in html_content
    assert magic_url in html_content
