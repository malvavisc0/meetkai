"""Tests for the email support bot (04-email-bot).

Covers: ``ingest_event`` (inbound email → agent turn → reply/silent),
SMTP reply path (mocked ``smtplib``), image attachment handling (mocked
``httpx.AsyncClient``), vision toggle, ``tell_hmac_key``/``tell_hmac_algorithm``,
and unsupported event rejection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai.agent.core import ChatResult
from kai.bots.email import Bot, EmailAction
from kai.bots.email.config import EmailSettings
from kai.bots.email.setup import BotConfig


def _make_bot(
    tmp_path: Path,
    *,
    vision: bool = False,
    max_attachment_bytes: int = 10 * 1024 * 1024,
) -> Bot:
    """Construct an email bot with minimal wiring for ingest_event tests."""
    bot = Bot(bot_dir=tmp_path)
    bot._config = BotConfig(language="English", timezone="UTC")
    bot._email = EmailSettings(
        control_host="0.0.0.0",
        control_port=8200,
        hmac_key="test-key",
        vision=vision,
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


def _chat_result(action: str, text: str | None = None) -> ChatResult:
    """Build a ChatResult whose .action has the given action/text."""
    return ChatResult(
        action=EmailAction(action=action, text=text),  # type: ignore[arg-type]
        reply=text or "",
        tool_calls=[],
    )


# ---------------------------------------------------------------------------
# ingest_event
# ---------------------------------------------------------------------------


class TestIngestEvent:
    @pytest.mark.asyncio
    async def test_unsupported_event_returns_false(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        result = await bot.ingest_event({"event": "some.other", "source": "a@b.com", "text": "hi"})
        assert result == {"ok": False}
        bot._agent.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_silent_action_returns_ok(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))
        result = await bot.ingest_event(
            {
                "event": "email.inbound",
                "source": "sender@example.com",
                "text": "what is KAI?",
                "metadata": {"subject": "question"},
            }
        )
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_exception_returns_false(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(side_effect=RuntimeError("boom"))
        result = await bot.ingest_event(
            {
                "event": "email.inbound",
                "source": "sender@example.com",
                "text": "hi",
            }
        )
        assert result == {"ok": False}

    @pytest.mark.asyncio
    async def test_passes_context_and_conversation_id(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))
        await bot.ingest_event(
            {
                "event": "email.inbound",
                "source": "alice@example.com",
                "text": "hello",
                "metadata": {"subject": "test"},
            }
        )
        call_kwargs = bot._agent.chat.call_args
        assert call_kwargs.kwargs["conversation_id"] == "alice@example.com"
        ctx = call_kwargs.kwargs["context"]
        assert ctx.sender_name == "alice@example.com"
        assert ctx.addressed_to_bot is True
        assert ctx.multi_party is False


# ---------------------------------------------------------------------------
# SMTP reply
# ---------------------------------------------------------------------------


class TestSmtpReply:
    @pytest.mark.asyncio
    async def test_reply_sends_email(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="Here is the answer."))

        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            result = await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "bob@example.com",
                    "text": "how do I deploy?",
                    "metadata": {"subject": "deployment question"},
                }
            )

        assert result == {"ok": True}
        mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
        # The reply email was sent
        server.send_message.assert_called_once()
        sent_msg = server.send_message.call_args.args[0]
        assert sent_msg["To"] == "bob@example.com"
        assert sent_msg["Subject"] == "Re: deployment question"
        assert "Here is the answer." in sent_msg.get_content()
        # From address is the operator's, not the LLM's
        assert "support@meetk.ai" in sent_msg["From"]
        # Default display name when the deployment hasn't configured one
        assert sent_msg["From"] == "Knowledgeable AI <support@meetk.ai>"

    @pytest.mark.asyncio
    async def test_reply_uses_configured_display_name(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._config = BotConfig(language="English", timezone="UTC", display_name="Acme Support")
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="Here is the answer."))

        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "bob@example.com",
                    "text": "hi",
                    "metadata": {"subject": "hi"},
                }
            )

        sent_msg = server.send_message.call_args.args[0]
        assert sent_msg["From"] == "Acme Support <support@meetk.ai>"

    @pytest.mark.asyncio
    async def test_reply_empty_subject_uses_default(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="hi"))

        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "bob@example.com",
                    "text": "hi",
                    "metadata": {"subject": ""},
                }
            )

        sent_msg = server.send_message.call_args.args[0]
        assert sent_msg["Subject"] == "Re: your email"

    @pytest.mark.asyncio
    async def test_smtp_not_configured_returns_false(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._smtp.smtp_enabled = False
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="hi"))

        result = await bot.ingest_event(
            {
                "event": "email.inbound",
                "source": "bob@example.com",
                "text": "hi",
                "metadata": {"subject": "test"},
            }
        )
        assert result == {"ok": False}

    @pytest.mark.asyncio
    async def test_smtp_send_failure_returns_false(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="hi"))

        with patch(
            "kai.agent.tools.email.smtplib.SMTP",
            side_effect=ConnectionRefusedError("no SMTP"),
        ):
            result = await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "bob@example.com",
                    "text": "hi",
                    "metadata": {"subject": "test"},
                }
            )
        assert result == {"ok": False}

    @pytest.mark.asyncio
    async def test_reply_uses_tls_and_auth(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="hi"))

        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "bob@example.com",
                    "text": "hi",
                    "metadata": {"subject": "test"},
                }
            )

        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@example.com", "pass")


# ---------------------------------------------------------------------------
# Image / attachment handling
# ---------------------------------------------------------------------------


class TestAttachments:
    def _mock_async_client(self, content: bytes | None = b""):
        """Build a mock httpx.AsyncClient that returns the given content.

        Returns a (mock_client_class, mock_get) pair. ``mock_get`` is the
        ``client.get`` mock for call assertions.
        """
        fake_resp = MagicMock()
        fake_resp.content = content
        fake_resp.raise_for_status = MagicMock()

        mock_get = AsyncMock(return_value=fake_resp)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_class = MagicMock(return_value=mock_client)
        return mock_class, mock_get

    @pytest.mark.asyncio
    async def test_image_attachment_downloaded_and_passed_to_agent(self, tmp_path, monkeypatch):
        bot = _make_bot(tmp_path, vision=True)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))

        mock_class, mock_get = self._mock_async_client(b"\x89PNG fake image bytes")
        with patch("kai.bots.email.httpx.AsyncClient", mock_class):
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "a@b.com",
                    "text": "see this image",
                    "metadata": {
                        "subject": "img",
                        "attachments": [
                            {
                                "url": "https://resend.example/att1",
                                "content_type": "image/png",
                                "filename": "shot.png",
                            }
                        ],
                    },
                }
            )

        mock_get.assert_called_once_with("https://resend.example/att1")
        images_arg = bot._agent.chat.call_args.kwargs["images"]
        assert images_arg is not None
        assert len(images_arg) == 1
        assert images_arg[0] == b"\x89PNG fake image bytes"

    @pytest.mark.asyncio
    async def test_vision_disabled_image_not_downloaded(self, tmp_path, monkeypatch):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.bots.email.httpx.AsyncClient") as mock_class:
            mock_class.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_class.return_value.__aexit__ = AsyncMock(return_value=False)
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "a@b.com",
                    "text": "see this image",
                    "metadata": {
                        "subject": "img",
                        "attachments": [
                            {
                                "url": "https://resend.example/att1",
                                "content_type": "image/png",
                                "filename": "shot.png",
                            }
                        ],
                    },
                }
            )

        mock_class.return_value.__aenter__.return_value.get.assert_not_called()
        assert bot._agent.chat.call_args.kwargs["images"] is None
        # The text passed to agent.chat should contain the tag
        enriched = bot._agent.chat.call_args.args[0]
        assert "[attachment: shot.png (image/png)]" in enriched

    @pytest.mark.asyncio
    async def test_non_image_attachment_tagged_not_downloaded(self, tmp_path, monkeypatch):
        bot = _make_bot(tmp_path, vision=True)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.bots.email.httpx.AsyncClient") as mock_class:
            mock_class.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_class.return_value.__aexit__ = AsyncMock(return_value=False)
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "a@b.com",
                    "text": "see attached",
                    "metadata": {
                        "subject": "doc",
                        "attachments": [
                            {
                                "url": "https://resend.example/doc.pdf",
                                "content_type": "application/pdf",
                                "filename": "report.pdf",
                            }
                        ],
                    },
                }
            )

        mock_class.return_value.__aenter__.return_value.get.assert_not_called()
        assert bot._agent.chat.call_args.kwargs["images"] is None
        enriched = bot._agent.chat.call_args.args[0]
        assert "[attachment: report.pdf (application/pdf)]" in enriched

    @pytest.mark.asyncio
    async def test_attachment_too_large_returns_none(self, tmp_path, monkeypatch):
        bot = _make_bot(tmp_path, vision=True, max_attachment_bytes=100)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))

        mock_class, mock_get = self._mock_async_client(b"x" * 200)
        with patch("kai.bots.email.httpx.AsyncClient", mock_class):
            await bot.ingest_event(
                {
                    "event": "email.inbound",
                    "source": "a@b.com",
                    "text": "big image",
                    "metadata": {
                        "subject": "img",
                        "attachments": [
                            {
                                "url": "https://resend.example/big.png",
                                "content_type": "image/png",
                                "filename": "big.png",
                            }
                        ],
                    },
                }
            )

        images_arg = bot._agent.chat.call_args.kwargs["images"]
        assert images_arg is None
        enriched = bot._agent.chat.call_args.args[0]
        assert "failed to download" in enriched


# ---------------------------------------------------------------------------
# handle_operator (console) — WAHA-style real-send parity, and display_name
# ---------------------------------------------------------------------------


class TestHandleOperator:
    @pytest.mark.asyncio
    async def test_reply_without_to_stays_local(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="local test reply"))

        result = await bot.handle_operator("test message")

        assert result.ok is True
        assert result.reply == "local test reply"
        assert result.actions == []
        bot._agent.record_assistant_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_with_to_sends_for_real_and_records_history(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="a real answer"))

        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            result = await bot.handle_operator("test message", to="alice@example.com")

        server.send_message.assert_called_once()
        sent_msg = server.send_message.call_args.args[0]
        assert sent_msg["To"] == "alice@example.com"
        bot._agent.record_assistant_message.assert_awaited_once_with(
            "alice@example.com", "a real answer"
        )
        assert result.ok is True
        assert result.actions == [
            {
                "tool": "send_reply",
                "target": "alice@example.com",
                "text": "a real answer",
                "ok": True,
            }
        ]
        assert result.reply == "a real answer"

    @pytest.mark.asyncio
    async def test_silent_action_never_sends_even_with_to(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            result = await bot.handle_operator("test message", to="alice@example.com")

        mock_smtp.assert_not_called()
        bot._agent.record_assistant_message.assert_not_called()
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_send_failure_reported_without_crashing(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = AsyncMock()
        bot._agent.chat = AsyncMock(return_value=_chat_result("reply", text="a real answer"))

        with patch(
            "kai.agent.tools.email.smtplib.SMTP",
            side_effect=ConnectionRefusedError("no SMTP"),
        ):
            result = await bot.handle_operator("test message", to="alice@example.com")

        assert result.ok is False
        assert "alice@example.com" in result.reply
        bot._agent.record_assistant_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_agent_returns_error(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._agent = None
        result = await bot.handle_operator("test message")
        assert result.ok is False


class TestDisplayName:
    def test_returns_configured_value(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot._config = BotConfig(display_name="Acme Support")
        assert bot.display_name() == "Acme Support"

    def test_returns_default_when_unconfigured(self, tmp_path):
        bot = Bot(bot_dir=tmp_path)
        assert bot.display_name() == "Knowledgeable AI"


# ---------------------------------------------------------------------------
# tell_hmac_key / tell_hmac_algorithm
# ---------------------------------------------------------------------------


class TestTellHmac:
    def test_tell_hmac_key_returns_configured(self, tmp_path):
        bot = _make_bot(tmp_path)
        assert bot.tell_hmac_key() == "test-key"

    def test_tell_hmac_algorithm_returns_configured(self, tmp_path):
        bot = _make_bot(tmp_path)
        assert bot.tell_hmac_algorithm() == "sha512"

    def test_tell_hmac_key_none_when_unconfigured(self, tmp_path):
        bot = Bot(bot_dir=tmp_path)
        assert bot.tell_hmac_key() is None

    def test_tell_hmac_algorithm_default_when_unconfigured(self, tmp_path):
        bot = Bot(bot_dir=tmp_path)
        assert bot.tell_hmac_algorithm() == "sha512"

    def test_tell_endpoint_returns_loopback(self, tmp_path):
        bot = _make_bot(tmp_path)
        endpoint = bot.tell_endpoint()
        assert endpoint == "http://127.0.0.1:8200"

    def test_tell_endpoint_none_when_unconfigured(self, tmp_path):
        bot = Bot(bot_dir=tmp_path)
        assert bot.tell_endpoint() is None
