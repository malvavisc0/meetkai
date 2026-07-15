import hashlib
import hmac as hmac_mod

import pytest
from httpx import ASGITransport, AsyncClient

from kai.bots.base import TellResult
from kai.bots.waha.webhook import create_webhook_app

_KEY = "test-secret"


def _sign(body: bytes, *, key: str = _KEY, algo=hashlib.sha512) -> str:
    return hmac_mod.new(key.encode(), body, algo).hexdigest()


def _headers(body: bytes, *, key: str = _KEY, algo=hashlib.sha512) -> dict:
    return {"Content-Type": "application/json", "X-Webhook-Hmac": _sign(body, key=key, algo=algo)}


@pytest.fixture
def webhook_app():
    return create_webhook_app(hmac_key=_KEY, webhook_path="/webhook/waha")


class TestWebhookEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, webhook_app):
        transport = ASGITransport(app=webhook_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_message_event_calls_handler(self):
        received = []

        async def handler(payload):
            received.append(payload)

        webhook_app_with_handler = create_webhook_app(
            hmac_key=_KEY, webhook_path="/webhook/waha", on_message=handler
        )
        transport = ASGITransport(app=webhook_app_with_handler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"event":"message","payload":{"from":"123@c.us","body":"hello"}}'
            resp = await client.post("/webhook/waha", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert len(received) == 1
            assert received[0]["payload"]["body"] == "hello"

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self):
        received = []

        async def handler(payload):
            received.append(payload)

        webhook_app_with_handler = create_webhook_app(
            hmac_key=_KEY, webhook_path="/webhook/waha", on_message=handler
        )
        transport = ASGITransport(app=webhook_app_with_handler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"event":"session.status","payload":{}}'
            resp = await client.post("/webhook/waha", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert len(received) == 0

    @pytest.mark.asyncio
    async def test_hmac_valid_signature(self):
        received = []
        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'

        async def handler(payload):
            received.append(payload)

        app = create_webhook_app(hmac_key=_KEY, webhook_path="/webhook/waha", on_message=handler)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/webhook/waha", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert len(received) == 1

    @pytest.mark.asyncio
    async def test_hmac_invalid_signature(self):
        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'
        app = create_webhook_app(hmac_key=_KEY, webhook_path="/webhook/waha")
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
        app = create_webhook_app(hmac_key=_KEY, webhook_path="/webhook/waha")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sha256_hmac_accepted(self):
        received = []
        body = b'{"event":"message","payload":{"from":"123@c.us","body":"hi"}}'

        async def handler(payload):
            received.append(payload)

        app = create_webhook_app(
            hmac_key=_KEY,
            hmac_algorithm="sha256",
            webhook_path="/webhook/waha",
            on_message=handler,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/webhook/waha", content=body, headers=_headers(body, algo=hashlib.sha256)
            )
            assert resp.status_code == 200
            assert len(received) == 1

    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self):
        app = create_webhook_app(hmac_key=_KEY, webhook_path="/webhook/waha")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b"x" * (2 * 1024 * 1024)
            resp = await client.post("/webhook/waha", content=body, headers=_headers(body))
            assert resp.status_code == 413


class TestWebhookHandlerErrors:
    @pytest.mark.asyncio
    async def test_handler_exception_returns_200(self):
        async def bad_handler(payload):
            raise RuntimeError("something broke")

        app = create_webhook_app(
            hmac_key=_KEY, webhook_path="/webhook/waha", on_message=bad_handler
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"event":"message","payload":{"from":"123@c.us"}}'
            resp = await client.post("/webhook/waha", content=body, headers=_headers(body))
            assert resp.status_code == 200


class TestTellRoute:
    @pytest.mark.asyncio
    async def test_tell_returns_structured_envelope(self):
        async def on_tell(message: str, *, persist: bool = False, to: str = "") -> TellResult:
            return TellResult(
                ok=True,
                actions=[{"tool": "send_to_group", "chat_id": "g@g.us", "ok": True}],
                reply=f"done: {message}",
            )

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":"send a joke","persist":false}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["reply"] == "done: send a joke"
            assert data["actions"][0]["tool"] == "send_to_group"

    @pytest.mark.asyncio
    async def test_tell_relays_persist_flag(self):
        captured = {}

        async def on_tell(message: str, *, persist: bool = False, to: str = "") -> TellResult:
            captured["persist"] = persist
            return TellResult(ok=True, reply="ack")

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":"steer","persist":true}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert captured["persist"] is True

    @pytest.mark.asyncio
    async def test_tell_relays_to_field(self):
        captured = {}

        async def on_tell(message: str, *, persist: bool = False, to: str = "") -> TellResult:
            captured["to"] = to
            return TellResult(ok=True, reply="ack")

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":"hi","persist":false,"to":"alice@example.com"}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert captured["to"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_tell_to_defaults_to_empty(self):
        captured = {}

        async def on_tell(message: str, *, persist: bool = False, to: str = "") -> TellResult:
            captured["to"] = to
            return TellResult(ok=True, reply="ack")

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":"hi","persist":false}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert captured["to"] == ""

    @pytest.mark.asyncio
    async def test_tell_rejects_invalid_signature(self):
        async def on_tell(message, *, persist: bool = False, to: str = ""):
            return TellResult(ok=True)

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/tell",
                content=b'{"message":"x"}',
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Hmac": "bad",
                },
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_tell_requires_message(self):
        async def on_tell(message, *, persist: bool = False, to: str = ""):
            return TellResult(ok=True)

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":""}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_tell_404_when_not_supported(self):
        app = create_webhook_app(hmac_key=_KEY)  # no on_tell
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":"x"}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_tell_handler_exception_returns_error_envelope(self):
        async def on_tell(message, *, persist: bool = False, to: str = ""):
            raise RuntimeError("boom")

        app = create_webhook_app(hmac_key=_KEY, on_tell=on_tell)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b'{"message":"x"}'
            resp = await client.post("/tell", content=body, headers=_headers(body))
            assert resp.status_code == 200
            assert resp.json()["ok"] is False


class TestStatusRoute:
    @pytest.mark.asyncio
    async def test_status_returns_snapshot(self):
        async def on_status():
            return {"session": {"name": "default", "status": "WORKING"}, "account": None}

        app = create_webhook_app(hmac_key=_KEY, on_status=on_status)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sig = _sign(b"")
            resp = await client.get("/status", headers={"X-Webhook-Hmac": sig})
            assert resp.status_code == 200
            data = resp.json()
            assert data["session"]["name"] == "default"
            assert data["session"]["status"] == "WORKING"

    @pytest.mark.asyncio
    async def test_status_rejects_invalid_signature(self):
        async def on_status():
            return {"session": None, "account": None}

        app = create_webhook_app(hmac_key=_KEY, on_status=on_status)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/status", headers={"X-Webhook-Hmac": "bad"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_status_404_when_not_supported(self):
        app = create_webhook_app(hmac_key=_KEY)  # no on_status
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/status", headers={"X-Webhook-Hmac": _sign(b"")})
            assert resp.status_code == 404


class TestClearRoute:
    @pytest.mark.asyncio
    async def test_clear_returns_ok(self):
        called = {"count": 0}

        async def on_clear():
            called["count"] += 1
            return {"ok": True}

        app = create_webhook_app(hmac_key=_KEY, on_clear=on_clear)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/clear", headers={"X-Webhook-Hmac": _sign(b"")})
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            assert called["count"] == 1

    @pytest.mark.asyncio
    async def test_clear_rejects_invalid_signature(self):
        async def on_clear():
            return {"ok": True}

        app = create_webhook_app(hmac_key=_KEY, on_clear=on_clear)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/clear", headers={"X-Webhook-Hmac": "bad"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_clear_404_when_not_supported(self):
        app = create_webhook_app(hmac_key=_KEY)  # no on_clear
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/clear", headers={"X-Webhook-Hmac": _sign(b"")})
            assert resp.status_code == 404
