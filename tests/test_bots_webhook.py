"""Tests for the shared bot webhook/tell/status/clear app."""

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from kai.bots.base import TellResult
from kai.bots.webhook import MAX_BODY_BYTES, create_webhook_app

KEY = "secret"


def _sign(body: bytes) -> str:
    return hmac.new(KEY.encode(), body, hashlib.sha512).hexdigest()


def _client(**handlers) -> TestClient:
    return TestClient(create_webhook_app(KEY, **handlers))


class TestHelpers:
    def test_health(self):
        response = _client().get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestWebhookRoute:
    def test_rejects_bad_signature(self):
        response = _client().post("/webhook", json={"event": "message"})
        assert response.status_code == 401

    def test_rejects_oversized_body(self):
        body = b"x" * (MAX_BODY_BYTES + 1)
        response = _client().post("/webhook", content=body, headers={"X-Webhook-Hmac": _sign(body)})
        assert response.status_code == 413

    def test_malformed_json_reports_error(self):
        body = b"{not-json"
        response = _client().post("/webhook", content=body, headers={"X-Webhook-Hmac": _sign(body)})
        assert response.json() == {"status": "error", "detail": "malformed body"}

    @pytest.mark.asyncio
    async def test_message_event_calls_handler(self):
        seen = []

        async def on_message(payload):
            seen.append(payload)

        response = _client(on_message=on_message).post(
            "/webhook",
            json={"event": "message", "payload": {"from": "alice"}},
            headers={"X-Webhook-Hmac": _sign(b"")},
        )
        assert response.status_code == 401

        body = b'{"event": "message", "payload": {"from": "alice"}}'
        response = _client(on_message=on_message).post(
            "/webhook", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.json() == {"status": "ok"}
        assert seen == [{"event": "message", "payload": {"from": "alice"}}]

    def test_handler_exception_still_returns_ok(self):
        async def on_message(payload):
            raise RuntimeError("boom")

        body = b'{"event": "message"}'
        response = _client(on_message=on_message).post(
            "/webhook", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.json() == {"status": "ok"}


class TestTellRoute:
    def test_missing_handler_is_404(self):
        assert _client().post("/tell", json={"message": "hi"}).status_code == 404

    def test_rejects_bad_signature(self):
        async def on_tell(message, *, persist=False):
            return TellResult(ok=True, reply="hi")

        assert _client(on_tell=on_tell).post("/tell", json={"message": "hi"}).status_code == 401

    def test_rejects_persist_non_bool(self):
        async def on_tell(message, *, persist=False):
            return TellResult(ok=True, reply="hi")

        body = b'{"message": "hi", "persist": "yes"}'
        response = _client(on_tell=on_tell).post(
            "/tell", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.status_code == 400

    def test_relays_message_and_persist(self):
        seen = {}

        async def on_tell(message, *, persist=False):
            seen.update(message=message, persist=persist)
            return TellResult(ok=True, reply="done")

        body = b'{"message": "hi", "persist": true}'
        response = _client(on_tell=on_tell).post(
            "/tell", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.json() == {"ok": True, "actions": [], "reply": "done"}
        assert seen == {"message": "hi", "persist": True}

    def test_handler_error_returns_failed_result(self):
        async def on_tell(message, *, persist=False):
            raise RuntimeError("boom")

        body = b'{"message": "hi"}'
        response = _client(on_tell=on_tell).post(
            "/tell", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "actions": [],
            "reply": "operator turn failed",
        }


class TestIngestRoute:
    def test_missing_handler_is_404(self):
        assert _client().post("/ingest", json={}).status_code == 404

    def test_rejects_bad_signature(self):
        async def on_ingest(payload):
            return {"ok": True}

        assert _client(on_ingest=on_ingest).post("/ingest", json={}).status_code == 401

    def test_unsupported_is_404(self):
        async def on_ingest(payload):
            raise NotImplementedError

        body = b"{}"
        response = _client(on_ingest=on_ingest).post(
            "/ingest", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.status_code == 404

    def test_success_returns_handler_payload(self):
        async def on_ingest(payload):
            return {"ok": True, "seen": payload}

        body = b'{"hello": "world"}'
        response = _client(on_ingest=on_ingest).post(
            "/ingest", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.json() == {"ok": True, "seen": {"hello": "world"}}

    def test_handler_error_is_500(self):
        async def on_ingest(payload):
            raise RuntimeError("boom")

        body = b"{}"
        response = _client(on_ingest=on_ingest).post(
            "/ingest", content=body, headers={"X-Webhook-Hmac": _sign(body)}
        )
        assert response.status_code == 500


class TestStatusAndClear:
    def test_status_missing_handler_is_404(self):
        assert _client().get("/status").status_code == 404

    def test_status_returns_snapshot(self):
        async def on_status():
            return {"state": "running"}

        response = _client(on_status=on_status).get(
            "/status", headers={"X-Webhook-Hmac": _sign(b"")}
        )
        assert response.json() == {"state": "running"}

    def test_status_error_is_500(self):
        async def on_status():
            raise RuntimeError("boom")

        response = _client(on_status=on_status).get(
            "/status", headers={"X-Webhook-Hmac": _sign(b"")}
        )
        assert response.status_code == 500

    def test_clear_missing_handler_is_404(self):
        assert _client().post("/clear").status_code == 404

    def test_clear_returns_result(self):
        async def on_clear():
            return {"cleared": True}

        response = _client(on_clear=on_clear).post("/clear", headers={"X-Webhook-Hmac": _sign(b"")})
        assert response.json() == {"cleared": True}

    def test_clear_error_is_500(self):
        async def on_clear():
            raise RuntimeError("boom")

        response = _client(on_clear=on_clear).post("/clear", headers={"X-Webhook-Hmac": _sign(b"")})
        assert response.status_code == 500
