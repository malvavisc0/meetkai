"""Tests for inbound email attachment handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.cockpit.email_helpers import _chat_result, _make_bot


class TestAttachments:
    @pytest.fixture(autouse=True)
    def _public_attachment_hosts(self, monkeypatch):
        """Resolve every attachment host as public; DNS is tested separately."""
        monkeypatch.setattr(
            "kai.bots.email.attachments.host_is_public", AsyncMock(return_value=True)
        )

    def _stream_cm(self, content: bytes, *, redirect_to: str | None = None):
        """Build an async context manager that mimics ``client.stream``."""
        resp = MagicMock()
        peer = MagicMock()
        peer.get_extra_info.return_value = ("8.8.8.8", 443)
        resp.extensions = {"network_stream": peer}
        resp.is_redirect = redirect_to is not None
        resp.headers = {"location": redirect_to} if redirect_to else {}
        resp.raise_for_status = MagicMock()

        async def aiter_bytes():
            yield content

        resp.aiter_bytes = aiter_bytes
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def _mock_async_client(self, content: bytes | None = b""):
        """Build a mock httpx.AsyncClient that streams the given content.

        Returns a (mock_client_class, mock_stream) pair. ``mock_stream`` is
        the ``client.stream`` mock for call assertions.
        """
        stream_cm = self._stream_cm(content if content is not None else b"")
        mock_stream = MagicMock(return_value=stream_cm)
        mock_client = MagicMock()
        mock_client.stream = mock_stream
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_class = MagicMock(return_value=mock_client)
        return mock_class, mock_stream

    @pytest.mark.asyncio
    async def test_image_attachment_downloaded_and_passed_to_agent(self, tmp_path):
        bot = _make_bot(tmp_path, vision=True)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        mock_class, mock_stream = self._mock_async_client(b"\x89PNG fake image bytes")
        with patch("kai.bots.email.attachments.httpx.AsyncClient", mock_class):
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

        mock_stream.assert_called_once_with("GET", "https://resend.example/att1")
        images_arg = bot.agent.chat.call_args.kwargs["images"]
        assert images_arg is not None
        assert len(images_arg) == 1
        assert images_arg[0] == b"\x89PNG fake image bytes"

    @pytest.mark.asyncio
    async def test_vision_disabled_image_not_downloaded(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.bots.email.attachments.httpx.AsyncClient") as mock_class:
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

        mock_class.return_value.__aenter__.return_value.stream.assert_not_called()
        assert bot.agent.chat.call_args.kwargs["images"] is None
        # The text passed to agent.chat should contain the tag
        enriched = bot.agent.chat.call_args.args[0]
        assert "[attachment: shot.png (image/png)]" in enriched

    @pytest.mark.asyncio
    async def test_non_image_attachment_tagged_not_downloaded(self, tmp_path):
        bot = _make_bot(tmp_path, vision=True)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.bots.email.attachments.httpx.AsyncClient") as mock_class:
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

        mock_class.return_value.__aenter__.return_value.stream.assert_not_called()
        assert bot.agent.chat.call_args.kwargs["images"] is None
        enriched = bot.agent.chat.call_args.args[0]
        assert "[attachment: report.pdf (application/pdf)]" in enriched

    @pytest.mark.asyncio
    async def test_attachment_too_large_returns_none(self, tmp_path):
        bot = _make_bot(tmp_path, vision=True, max_attachment_bytes=100)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        mock_class, _ = self._mock_async_client(b"x" * 200)
        with patch("kai.bots.email.attachments.httpx.AsyncClient", mock_class):
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

        images_arg = bot.agent.chat.call_args.kwargs["images"]
        assert images_arg is None
        enriched = bot.agent.chat.call_args.args[0]
        assert "failed to download" in enriched

    @pytest.mark.asyncio
    async def test_attachment_redirect_to_public_host_is_followed(self, tmp_path):
        bot = _make_bot(tmp_path, vision=True)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        mock_stream = MagicMock(
            side_effect=[
                self._stream_cm(b"", redirect_to="https://cdn.example/img.png"),
                self._stream_cm(b"\x89PNG"),
            ]
        )
        mock_client = MagicMock()
        mock_client.stream = mock_stream
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_class = MagicMock(return_value=mock_client)

        with patch("kai.bots.email.attachments.httpx.AsyncClient", mock_class):
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

        assert mock_stream.call_args_list[1].args == (
            "GET",
            "https://cdn.example/img.png",
        )
        assert bot.agent.chat.call_args.kwargs["images"] == [b"\x89PNG"]

    @pytest.mark.asyncio
    async def test_attachment_private_host_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kai.bots.email.attachments.host_is_public", AsyncMock(return_value=False)
        )
        bot = _make_bot(tmp_path, vision=True)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.bots.email.attachments.httpx.AsyncClient") as mock_class:
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

        mock_class.return_value.__aenter__.return_value.stream.assert_not_called()
        assert bot.agent.chat.call_args.kwargs["images"] is None
        assert "failed to download" in bot.agent.chat.call_args.args[0]

    @pytest.mark.asyncio
    async def test_attachment_redirect_to_private_host_is_rejected(self, tmp_path, monkeypatch):
        async def allow_public_only(host: str) -> bool:
            return host == "resend.example"

        monkeypatch.setattr("kai.bots.email.attachments.host_is_public", allow_public_only)
        bot = _make_bot(tmp_path, vision=True)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        mock_stream = MagicMock(
            return_value=self._stream_cm(b"", redirect_to="https://private.example/img.png")
        )
        mock_client = MagicMock()
        mock_client.stream = mock_stream
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_class = MagicMock(return_value=mock_client)

        with patch("kai.bots.email.attachments.httpx.AsyncClient", mock_class):
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

        assert mock_stream.call_count == 1
        assert bot.agent.chat.call_args.kwargs["images"] is None
        assert "failed to download" in bot.agent.chat.call_args.args[0]

    @pytest.mark.asyncio
    async def test_attachment_http_url_is_rejected(self, tmp_path):
        bot = _make_bot(tmp_path, vision=True)
        bot.agent = AsyncMock()
        bot.agent.chat = AsyncMock(return_value=_chat_result("silent"))

        with patch("kai.bots.email.attachments.httpx.AsyncClient") as mock_class:
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
                                "url": "http://resend.example/att1",
                                "content_type": "image/png",
                                "filename": "shot.png",
                            }
                        ],
                    },
                }
            )

        mock_class.return_value.__aenter__.return_value.stream.assert_not_called()
        assert bot.agent.chat.call_args.kwargs["images"] is None


class TestHostValidation:
    @pytest.mark.asyncio
    async def test_dns_failure_rejects_host(self, monkeypatch):
        import socket

        def fail(*args, **kwargs):
            raise socket.gaierror("no such host")

        monkeypatch.setattr("kai.bots.email.attachments.socket.getaddrinfo", fail)

        from kai.bots.email.attachments import host_is_public

        assert await host_is_public("missing.invalid") is False

    # ---------------------------------------------------------------------------
    # handle_operator — agent decides via structured action
    # ---------------------------------------------------------------------------

    def test_peer_is_public_rejects_private_address(self):
        from kai.bots.email.attachments import peer_is_public

        resp = MagicMock()
        stream = MagicMock()
        stream.get_extra_info.return_value = ("127.0.0.1", 443)
        resp.extensions = {"network_stream": stream}
        assert peer_is_public(resp) is False

    def test_peer_is_public_accepts_global_address(self):
        from kai.bots.email.attachments import peer_is_public

        resp = MagicMock()
        stream = MagicMock()
        stream.get_extra_info.return_value = ("8.8.8.8", 443)
        resp.extensions = {"network_stream": stream}
        assert peer_is_public(resp) is True

    @pytest.mark.asyncio
    async def test_host_is_public_accepts_all_global_addresses(self, monkeypatch):
        from kai.bots.email.attachments import host_is_public

        def resolve(*args, **kwargs):
            return [
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("1.1.1.1", 443)),
            ]

        monkeypatch.setattr("kai.bots.email.attachments.socket.getaddrinfo", resolve)
        assert await host_is_public("example.com") is True

    @pytest.mark.asyncio
    async def test_host_is_public_rejects_mixed_public_and_private(self, monkeypatch):
        from kai.bots.email.attachments import host_is_public

        def resolve(*args, **kwargs):
            return [
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("127.0.0.1", 443)),
            ]

        monkeypatch.setattr("kai.bots.email.attachments.socket.getaddrinfo", resolve)
        assert await host_is_public("example.com") is False

    @pytest.mark.asyncio
    async def test_host_is_public_rejects_empty_resolution(self, monkeypatch):
        from kai.bots.email.attachments import host_is_public

        monkeypatch.setattr("kai.bots.email.attachments.socket.getaddrinfo", lambda *a, **k: [])
        assert await host_is_public("example.com") is False

    @pytest.mark.asyncio
    async def test_read_capped_body_aborts_when_over_limit(self):
        from kai.bots.email.attachments import read_capped_body

        resp = MagicMock()

        async def aiter_bytes():
            yield b"123"
            yield b"456"

        resp.aiter_bytes = aiter_bytes
        assert await read_capped_body(resp, max_bytes=4) is None

    def test_peer_is_public_fails_closed_without_stream(self):
        from kai.bots.email.attachments import peer_is_public

        resp = MagicMock()
        resp.extensions = {}
        assert peer_is_public(resp) is False

    def test_peer_is_public_fails_closed_without_address(self):
        from kai.bots.email.attachments import peer_is_public

        resp = MagicMock()
        stream = MagicMock()
        stream.get_extra_info.return_value = None
        resp.extensions = {"network_stream": stream}
        assert peer_is_public(resp) is False
