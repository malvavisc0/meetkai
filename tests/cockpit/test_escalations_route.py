"""Tests for the cockpit escalation routes: ingest webhook, dashboard, badge.

The bot→cockpit forwarding works like this: a bot's ``escalate()`` tool
persists the escalation locally AND fires ``BaseBot.on_escalation``, which
calls ``forward_to_cockpit`` to POST the escalation to the cockpit's
``/api/escalations`` webhook. The cockpit stores it in its own
``EscalationStore`` (wired in ``create_app``) so the dashboard + sidebar
badge read a single source of truth. These tests exercise that cockpit side.
"""

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider

_ESCALATION_PAYLOAD = {
    "id": "esc-test-1",
    "chat_id": "120363@g.us",
    "conversation_id": "120363@g.us",
    "reason": "Customer wants a human agent",
    "severity": "high",
    "summary": "Customer is frustrated after a billing error",
    "created_at": "2026-07-17T18:05:58+00:00",
    "resolved": False,
    "resolved_at": None,
    "resolved_by": None,
}


def _login(client, db, user) -> None:
    """Drive the request→approve→magic-link flow to authenticate ``client``."""
    tokens.create_login_request(db, user.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(user.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302


class TestIngestEndpoint:
    def test_post_stores_escalation(self, client):
        resp = client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert body["escalation"]["id"] == "esc-test-1"

        listing = client.get("/api/escalations").json()
        assert len(listing["escalations"]) == 1
        assert listing["escalations"][0]["reason"] == "Customer wants a human agent"

    def test_post_appears_in_active(self, client):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        active = client.get("/api/escalations/active").json()
        assert active["count"] == 1
        assert active["escalations"][0]["severity"] == "high"

    def test_post_preserves_bot_generated_id_and_created_at(self, client):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        esc = client.get("/api/escalations").json()["escalations"][0]
        assert esc["id"] == "esc-test-1"
        # created_at round-trips as an ISO 8601 UTC timestamp (Pydantic's JSON
        # mode may emit "Z" instead of "+00:00" — both are equivalent).
        from datetime import datetime

        parsed = datetime.fromisoformat(esc["created_at"].replace("Z", "+00:00"))
        assert parsed.year == 2026 and parsed.month == 7 and parsed.day == 17

    def test_post_rejects_non_object_payload(self, client):
        # The endpoint parses the body itself (Request, not a dict annotation),
        # so a non-dict body is rejected with 400, not FastAPI's 422.
        resp = client.post("/api/escalations", json=[1, 2, 3])
        assert resp.status_code == 400

    def test_post_rejects_missing_reason(self, client):
        bad = dict(_ESCALATION_PAYLOAD)
        del bad["reason"]
        resp = client.post("/api/escalations", json=bad)
        assert resp.status_code == 400
        assert "invalid escalation" in resp.json()["error"]

    def test_post_rejects_bad_severity(self, client):
        bad = dict(_ESCALATION_PAYLOAD)
        bad["severity"] = "extreme"
        resp = client.post("/api/escalations", json=bad)
        assert resp.status_code == 400


class TestIngestAuth:
    def test_rejects_missing_token_when_secret_set(self, client, monkeypatch):
        monkeypatch.setenv("KAI_COCKPIT_ESCALATION_SECRET", "s3cret")
        resp = client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        assert resp.status_code == 401

    def test_rejects_wrong_token(self, client, monkeypatch):
        monkeypatch.setenv("KAI_COCKPIT_ESCALATION_SECRET", "s3cret")
        resp = client.post(
            "/api/escalations",
            json=_ESCALATION_PAYLOAD,
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_accepts_correct_token(self, client, monkeypatch):
        monkeypatch.setenv("KAI_COCKPIT_ESCALATION_SECRET", "s3cret")
        resp = client.post(
            "/api/escalations",
            json=_ESCALATION_PAYLOAD,
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status_code == 201


class TestDashboardRoute:
    def test_dashboard_requires_auth(self, client):
        resp = client.get("/escalations", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    def test_dashboard_shows_active_escalation(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        _login(client, db, user)
        resp = client.get("/escalations")
        assert resp.status_code == 200
        assert "Customer wants a human agent" in resp.text
        assert "active escalation" in resp.text

    def test_dashboard_shows_resolved_in_history(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        _login(client, db, user)
        client.post("/escalations/esc-test-1/resolve", follow_redirects=False)
        resp = client.get("/escalations")
        assert resp.status_code == 200
        assert "Nothing needs attention" in resp.text
        assert "Customer wants a human agent" in resp.text


class TestResolveEndpoint:
    def test_resolve_marks_resolved(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        _login(client, db, user)
        resp = client.post("/api/escalations/esc-test-1/resolve")
        assert resp.json()["ok"] is True
        active = client.get("/api/escalations/active").json()
        assert active["count"] == 0

    def test_resolve_records_resolved_by(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        _login(client, db, user)
        client.post("/api/escalations/esc-test-1/resolve")
        esc = client.get("/api/escalations").json()["escalations"][0]
        assert esc["resolved"] is True
        assert esc["resolved_by"] == user.email

    def test_resolve_nonexistent_returns_error(self, client, db, user):
        _login(client, db, user)
        resp = client.post("/api/escalations/esc-does-not-exist/resolve")
        assert resp.json()["ok"] is False

    def test_resolve_twice_second_fails(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        _login(client, db, user)
        assert client.post("/api/escalations/esc-test-1/resolve").json()["ok"] is True
        assert client.post("/api/escalations/esc-test-1/resolve").json()["ok"] is False


class TestSidebarBadge:
    def test_badge_hidden_when_no_active_escalations(self, client, db, user):
        _login(client, db, user)
        # Any authenticated page renders base.html with the topbar.
        resp = client.get("/escalations")
        assert "topbar__badge" not in resp.text

    def test_badge_shows_count(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        # Second escalation to verify the count is > 1.
        second = dict(_ESCALATION_PAYLOAD)
        second["id"] = "esc-test-2"
        second["reason"] = "Second issue"
        client.post("/api/escalations", json=second)
        _login(client, db, user)
        resp = client.get("/escalations")
        assert "topbar__badge" in resp.text
        assert ">2<" in resp.text

    def test_badge_disappears_after_resolve(self, client, db, user):
        client.post("/api/escalations", json=_ESCALATION_PAYLOAD)
        _login(client, db, user)
        client.post("/escalations/esc-test-1/resolve", follow_redirects=False)
        resp = client.get("/escalations")
        assert "topbar__badge" not in resp.text

    def test_nav_link_present(self, client, db, user):
        _login(client, db, user)
        resp = client.get("/escalations")
        assert 'href="/escalations"' in resp.text
        assert "escalations" in resp.text.lower()
