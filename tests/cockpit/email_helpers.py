"""Shared construction helpers for email bot tests."""

from pathlib import Path
from unittest.mock import MagicMock

from kai.agent.core import ChatResult
from kai.bots.email import Bot, EmailAction
from kai.bots.email.config import EmailSettings
from kai.bots.email.setup import BotConfig


def _make_bot(
    tmp_path: Path,
    *,
    vision: bool = False,
    video: bool = False,
    max_attachment_bytes: int = 10 * 1024 * 1024,
) -> Bot:
    """Construct an email bot with minimal wiring for ingest_event tests."""
    bot = Bot(bot_dir=tmp_path)
    bot._config = BotConfig(language="English", timezone="UTC", vision=vision, video=video)
    bot._email = EmailSettings(
        control_host="0.0.0.0",
        control_port=8200,
        hmac_key="test-key",
        max_attachment_bytes=max_attachment_bytes,
    )
    bot._smtp = MagicMock()
    bot._smtp.smtp_enabled = True
    bot._smtp.host = "smtp.example.com"
    bot._smtp.port = 587
    bot._smtp.username = "user@example.com"
    bot._smtp.password = "pass"
    bot._smtp.from_address = "support@meetk.ai"
    bot._smtp.use_tls = True
    return bot


def _chat_result(action: str, text: str | None = None, target: str | None = None) -> ChatResult:
    """Build a ChatResult whose .action has the given action/text/target."""
    return ChatResult(
        action=EmailAction(action=action, text=text, target=target),  # type: ignore[arg-type]
        reply=text or "",
        tool_calls=[],
    )
