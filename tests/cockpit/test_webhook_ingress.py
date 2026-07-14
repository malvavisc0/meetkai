"""Tests for cockpit-level webhook ingress (Fix 04).

Since WEBHOOK_TYPES starts empty, a fake type is seeded in the test fixture
(a WebhookType with a trivial verify_signature returning True and a parse
returning a fixed NormalizedMessage) so the route is exercised without a real
provider.
"""

import hmac
import json

import pytest

from kai.bots.base import BaseBot
from kai.cockpit.bots import BOT_TYPES, BotType
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection, Deployment, User
from kai.cockpit.naming import kai_slug_for
from kai.cockpit.webhooks import WEBHOOK_TYPES, NormalizedMessage, WebhookType


@pytest.fixture
def alice(db):
    u = User(
        email="alice@test.com",
        language="English",
        timezone="UTC",
        hmac_key="alice-hmac-key",
        created_at="now",
        is_disabled=False,
        kai_slug=kai_slug_for("alice@test.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _whatsapp_conn(user_id: int) -> Connection:
    return Connection(
        user_id=user_id,
        service="whatsapp",
        status="connected",
        config={
            "waha_session": "kai-alice",
            "waha_webhook_port": 8101,
            "waha_webhook_path": "/webhook/whatsapp-1",
        },
        created_at="now",
        updated_at="now",
    )


_FAKE_TYPE = WebhookType(
    name="test",
    verify_signature=lambda request, body, secret: True,
    parse=lambda payload, cfg: NormalizedMessage(
        source="test-provider", text=payload.get("text", ""), event="message"
    ),
)


@pytest.fixture
def fake_webhook_type(monkeypatch):
    # Register both a WebhookType (route verify/parse) and a matching
    # WebhookConnectionType (so decrypt_config knows "test" carries a
    # signing_secret — mirroring how "resend" is paired with its
    # WebhookConnectionType in production).
    from kai.cockpit.bots import WEBHOOK_CONNECTION_TYPES, WebhookConnectionType
    from kai.cockpit.webhooks import _clear_seen_nonces

    monkeypatch.setitem(WEBHOOK_TYPES, "test", _FAKE_TYPE)
    monkeypatch.setitem(
        WEBHOOK_CONNECTION_TYPES,
        "test",
        WebhookConnectionType(
            service="test",
            label="Test",
            fields=[],
            webhook_type="test",
            secret_fields=["signing_secret"],
        ),
    )
    _clear_seen_nonces()
    yield
    WEBHOOK_TYPES.pop("test", None)
    WEBHOOK_CONNECTION_TYPES.pop("test", None)
    _clear_seen_nonces()


@pytest.fixture
def fake_connection(db, alice):
    """A Connection row for the fake 'test' webhook type — the reordered
    route loads a per-operator connection before verification, so the route
    404s without one."""
    from datetime import UTC, datetime

    conn = Connection(
        user_id=alice.id,
        service="test",
        status="connected",
        config={"signing_secret": "test-secret"},
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


class TestRouteStatusCodes:
    def test_unknown_type_returns_404(self, client, alice, fake_webhook_type):
        r = client.post(
            f"/webhook/{alice.kai_slug}/nonexistent",
            json={"text": "hi"},
        )
        assert r.status_code == 404

    def test_known_type_unknown_slug_returns_404(self, client, alice, fake_webhook_type):
        r = client.post(
            "/webhook/kai-v001-nobody_at_test_com/test",
            json={"text": "hi"},
        )
        assert r.status_code == 404

    def test_bad_signature_returns_401(
        self, client, alice, fake_webhook_type, fake_connection, monkeypatch
    ):
        monkeypatch.setitem(
            WEBHOOK_TYPES,
            "test",
            WebhookType(
                name="test",
                verify_signature=lambda request, body, secret: False,
                parse=_FAKE_TYPE.parse,
            ),
        )
        r = client.post(
            f"/webhook/{alice.kai_slug}/test",
            json={"text": "hi"},
        )
        assert r.status_code == 401

    def test_no_running_deployment_returns_404(
        self, client, alice, fake_webhook_type, fake_connection, monkeypatch
    ):
        fake_bt = BotType(
            name="emailbot",
            feature_flags=[],
            required_connections=["test"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "emailbot", fake_bt)

        r = client.post(
            f"/webhook/{alice.kai_slug}/test",
            json={"text": "hi"},
        )
        assert r.status_code == 404

    def test_running_deployment_forwards_event(
        self, client, db, alice, fake_webhook_type, fake_connection, monkeypatch
    ):
        fake_bt = BotType(
            name="emailbot",
            feature_flags=[],
            required_connections=["test"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "emailbot", fake_bt)

        dep = Deployment(
            user_id=alice.id,
            bot_type="emailbot",
            run_id="fake-run",
            status="running",
            desired_state="running",
            voice="af_heart",
            goal="answer email",
            language="English",
            feature_flags={},
            settings={},
            created_at="now",
            updated_at="now",
        )
        db.add(dep)
        db.commit()

        forwarded: list[tuple[str, bytes]] = []

        def fake_forward(self, deployment, path, body):
            forwarded.append((path, body))
            return True

        monkeypatch.setattr(DeploymentsService, "forward_event", fake_forward)

        r = client.post(
            f"/webhook/{alice.kai_slug}/test",
            json={"text": "hello world"},
        )

        assert r.status_code == 202
        assert len(forwarded) == 1
        assert forwarded[0][0] == "/ingest"
        payload = json.loads(forwarded[0][1])
        assert payload["source"] == "test-provider"
        assert payload["text"] == "hello world"
        assert payload["event"] == "message"

    def test_bot_rejects_returns_502(
        self, client, db, alice, fake_webhook_type, fake_connection, monkeypatch
    ):
        fake_bt = BotType(
            name="emailbot",
            feature_flags=[],
            required_connections=["test"],
            supported_connections=[],
        )
        monkeypatch.setitem(BOT_TYPES, "emailbot", fake_bt)

        dep = Deployment(
            user_id=alice.id,
            bot_type="emailbot",
            run_id="fake-run",
            status="running",
            desired_state="running",
            voice="af_heart",
            goal="answer email",
            language="English",
            feature_flags={},
            settings={},
            created_at="now",
            updated_at="now",
        )
        db.add(dep)
        db.commit()

        monkeypatch.setattr(DeploymentsService, "forward_event", lambda self, d, p, b: False)

        r = client.post(
            f"/webhook/{alice.kai_slug}/test",
            json={"text": "hi"},
        )
        assert r.status_code == 502


class TestForwardEvent:
    def test_returns_false_when_no_run_record(self, db, alice):
        svc = DeploymentsService(db)
        dep = Deployment(
            user_id=alice.id,
            bot_type="waha",
            run_id=None,
            status="stopped",
            desired_state="stopped",
            voice="af_heart",
            goal="x",
            language="English",
            feature_flags={},
            settings={},
            created_at="now",
            updated_at="now",
        )
        db.add(dep)
        db.commit()
        assert svc.forward_event(dep, "/ingest", b"{}") is False

    def test_returns_false_when_call_bot_fails(self, db, alice, monkeypatch):
        svc = DeploymentsService(db)
        dep = Deployment(
            user_id=alice.id,
            bot_type="waha",
            run_id="fake-run",
            status="running",
            desired_state="running",
            voice="af_heart",
            goal="x",
            language="English",
            feature_flags={},
            settings={},
            created_at="now",
            updated_at="now",
        )
        db.add(dep)
        db.commit()

        monkeypatch.setattr(svc, "_resolve_run", lambda d: object())
        monkeypatch.setattr(svc, "_call_bot", lambda *a, **k: {"ok": False, "error": "boom"})
        assert svc.forward_event(dep, "/ingest", b"{}") is False

    def test_returns_true_on_success(self, db, alice, monkeypatch):
        svc = DeploymentsService(db)
        dep = Deployment(
            user_id=alice.id,
            bot_type="waha",
            run_id="fake-run",
            status="running",
            desired_state="running",
            voice="af_heart",
            goal="x",
            language="English",
            feature_flags={},
            settings={},
            created_at="now",
            updated_at="now",
        )
        db.add(dep)
        db.commit()

        monkeypatch.setattr(svc, "_resolve_run", lambda d: object())
        monkeypatch.setattr(svc, "_call_bot", lambda *a, **k: {"status": "ok"})
        assert svc.forward_event(dep, "/ingest", b"{}") is True


class TestIngestRoute:
    def test_ingest_404_when_not_supported(self):
        from kai.bots.waha.webhook import create_webhook_app

        app = create_webhook_app(hmac_key="k")
        client = pytest.importorskip("starlette.testclient").TestClient(app)
        r = client.post("/ingest", json={"text": "hi"})
        assert r.status_code == 404

    def test_ingest_hmac_verified(self):
        from kai.bots.waha.webhook import create_webhook_app

        received: list[dict] = []

        async def on_ingest(payload):
            received.append(payload)
            return {"ok": True}

        key = "test-key"
        app = create_webhook_app(hmac_key=key, on_ingest=on_ingest)
        client = pytest.importorskip("starlette.testclient").TestClient(app)

        body = json.dumps({"text": "hi"}).encode()
        sig = hmac.new(key.encode(), body, "sha512").hexdigest()
        r = client.post("/ingest", content=body, headers={"X-Webhook-Hmac": sig})
        assert r.status_code == 200
        assert received == [{"text": "hi"}]

    def test_ingest_rejects_bad_signature(self):
        from kai.bots.waha.webhook import create_webhook_app

        async def on_ingest(payload):
            return {"ok": True}

        app = create_webhook_app(hmac_key="real-key", on_ingest=on_ingest)
        client = pytest.importorskip("starlette.testclient").TestClient(app)

        r = client.post(
            "/ingest",
            content=json.dumps({"text": "hi"}).encode(),
            headers={"X-Webhook-Hmac": "wrong-signature"},
        )
        assert r.status_code == 401


class TestBaseBotIngestEvent:
    def test_default_raises_not_implemented(self):
        from pathlib import Path

        class DummyBot(BaseBot):
            name = "dummy"

            async def run(self): ...

        bot = DummyBot(Path("/tmp"))
        import asyncio

        with pytest.raises(NotImplementedError, match="does not implement ingest_event"):
            asyncio.run(bot.ingest_event({}))
