"""Per-user escalation scoping (Workstream A3).

Covers the dashboard + resolve routes' owner-scoping: a logged-in operator
must not see or resolve another operator's escalations via the cockpit
session. The bearer-authed ``/api/escalations`` ingest endpoint stays
unscoped (any bot can POST to it); that is the same model as before A3.
"""

import secrets
from datetime import UTC, datetime

from tests.cockpit.helpers import csrf_post

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import User
from kai.cockpit.naming import kai_slug_for


def _make_user(db, email: str) -> User:
    u = User(
        email=email,
        language="English",
        timezone="UTC",
        hmac_key=secrets.token_hex(32),
        created_at=datetime.now(UTC).isoformat(),
        kai_slug=kai_slug_for(email),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _login(client, db, user) -> None:
    tokens.create_login_request(db, user.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(user.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302


def _payload(escalation_id: str, owner_slug: str, reason: str = "needs human") -> dict:
    return {
        "id": escalation_id,
        "chat_id": "120363@g.us",
        "conversation_id": "120363@g.us",
        "reason": reason,
        "severity": "high",
        "summary": "synthetic test escalation",
        "user_id": owner_slug,
        "created_at": "2026-07-24T12:00:00+00:00",
        "resolved": False,
        "resolved_at": None,
        "resolved_by": None,
    }


class TestDashboardScope:
    def test_user_does_not_see_another_users_escalation(self, client, db, user):
        alice = _make_user(db, "alice@test.com")
        client.post("/api/escalations", json=_payload("esc-alice-1", alice.kai_slug))
        _login(client, db, user)
        resp = client.get("/escalations")
        assert resp.status_code == 200
        assert "needs human" not in resp.text

    def test_user_sees_own_escalation(self, client, db, user):
        client.post("/api/escalations", json=_payload("esc-bob-1", user.kai_slug))
        _login(client, db, user)
        resp = client.get("/escalations")
        assert resp.status_code == 200
        assert "needs human" in resp.text

    def test_dashboard_only_renders_owners_active_count(self, client, db, user):
        alice = _make_user(db, "alice@test.com")
        client.post(
            "/api/escalations", json=_payload("esc-alice-1", alice.kai_slug, "Alice issue 1")
        )
        client.post(
            "/api/escalations", json=_payload("esc-alice-2", alice.kai_slug, "Alice issue 2")
        )
        client.post("/api/escalations", json=_payload("esc-bob-1", user.kai_slug, "Bob issue"))
        _login(client, db, user)
        resp = client.get("/escalations")
        assert "Bob issue" in resp.text
        assert "Alice issue 1" not in resp.text
        assert "Alice issue 2" not in resp.text


class TestResolveScope:
    def test_user_cannot_resolve_another_users_escalation(self, client, db, user):
        alice = _make_user(db, "alice@test.com")
        client.post("/api/escalations", json=_payload("esc-alice-1", alice.kai_slug))
        _login(client, db, user)
        resp = csrf_post(client, "/api/escalations/esc-alice-1/resolve")
        assert resp.json()["ok"] is False
        active = client.get("/api/escalations/active").json()
        assert active["count"] == 1

    def test_form_resolve_redirects_without_mutating(self, client, db, user):
        alice = _make_user(db, "alice@test.com")
        client.post("/api/escalations", json=_payload("esc-alice-1", alice.kai_slug))
        _login(client, db, user)
        resp = csrf_post(client, "/escalations/esc-alice-1/resolve", follow_redirects=False)
        assert resp.status_code == 303
        active = client.get("/api/escalations/active").json()
        assert active["count"] == 1

    def test_user_can_resolve_own_escalation(self, client, db, user):
        client.post("/api/escalations", json=_payload("esc-bob-1", user.kai_slug))
        _login(client, db, user)
        resp = csrf_post(client, "/api/escalations/esc-bob-1/resolve")
        assert resp.json()["ok"] is True


class TestBearingApiScope:
    def test_bearer_api_returns_all_escalations(self, client, db, user):
        alice = _make_user(db, "alice@test.com")
        client.post("/api/escalations", json=_payload("esc-alice-1", alice.kai_slug))
        client.post("/api/escalations", json=_payload("esc-bob-1", user.kai_slug))
        resp = client.get("/api/escalations")
        ids = {e["id"] for e in resp.json()["escalations"]}
        assert ids == {"esc-alice-1", "esc-bob-1"}
