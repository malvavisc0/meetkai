import pytest
from httpx import ASGITransport, AsyncClient

from kai.bots.waha.webhook import create_webhook_app


@pytest.fixture
def webhook_app():
    return create_webhook_app(hmac_key=None, webhook_path="/webhook/waha")


class TestWebhookEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, webhook_app):
        transport = ASGITransport(app=webhook_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_message_event_calls_handler(self, webhook_app):
        received = []

        async def handler(payload):
            received.append(payload)

        webhook_app_with_handler = create_webhook_app(
            hmac_key=None, webhook_path="/webhook/waha", on_message=handler
        )
        transport = ASGITransport(app=webhook_app_with_handler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                json={
                    "event": "message",
                    "payload": {"from": "123@c.us", "body": "hello"},
                },
            )
            assert resp.status_code == 200
            assert len(received) == 1
            assert received[0]["payload"]["body"] == "hello"

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self, webhook_app):
        received = []

        async def handler(payload):
            received.append(payload)

        webhook_app_with_handler = create_webhook_app(
            hmac_key=None, webhook_path="/webhook/waha", on_message=handler
        )
        transport = ASGITransport(app=webhook_app_with_handler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                json={"event": "session.status", "payload": {}},
            )
            assert resp.status_code == 200
            assert len(received) == 0

    @pytest.mark.asyncio
    async def test_hmac_valid_signature(self):
        import hashlib
        import hmac as hmac_mod

        received = []
        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'
        signature = hmac_mod.new(b"test-secret", body, hashlib.sha512).hexdigest()

        async def handler(payload):
            received.append(payload)

        app = create_webhook_app(
            hmac_key="test-secret", webhook_path="/webhook/waha", on_message=handler
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Hmac": signature,
                },
            )
            assert resp.status_code == 200
            assert len(received) == 1

    @pytest.mark.asyncio
    async def test_hmac_invalid_signature(self):
        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'
        app = create_webhook_app(hmac_key="test-secret", webhook_path="/webhook/waha")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Hmac": "wrong-signature",
                },
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_hmac_missing_signature(self):
        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'
        app = create_webhook_app(hmac_key="test-secret", webhook_path="/webhook/waha")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 401


class TestWebhookHandlerErrors:
    @pytest.mark.asyncio
    async def test_handler_exception_returns_200(self):
        async def bad_handler(payload):
            raise RuntimeError("something broke")

        app = create_webhook_app(
            hmac_key=None, webhook_path="/webhook/waha", on_message=bad_handler
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                json={"event": "message", "payload": {"from": "123@c.us"}},
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_sha256_hmac_accepted(self):
        import hashlib
        import hmac as hmac_mod

        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'
        signature = hmac_mod.new(b"test-secret", body, hashlib.sha256).hexdigest()

        received = []

        async def handler(payload):
            received.append(payload)

        app = create_webhook_app(
            hmac_key="test-secret",
            hmac_algorithm="sha256",
            webhook_path="/webhook/waha",
            on_message=handler,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Hmac": signature,
                },
            )
            assert resp.status_code == 200
            assert len(received) == 1

    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self):
        app = create_webhook_app(hmac_key=None, webhook_path="/webhook/waha")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                content=b"x" * (2 * 1024 * 1024),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 413
