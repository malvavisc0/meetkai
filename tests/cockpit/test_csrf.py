"""CSRF (Workstream A1) protection tests.

The cockpit's CSRF middleware (double-submit / synchronizer token) must:

1. Reject any unsafe browser request that lacks a matching ``_csrf`` form
   field or ``X-CSRF-Token`` header.
2. Accept requests that carry the token from the session.
3. Leave the machine-consumer webhook (``/webhook/...``) and the bearer
   ingress (``/api/escalations`` ingest) untouched — they authenticate by
   signature/HMAC, not session cookie, so CSRF does not apply.
4. Leave ``/logout`` (GET, safe method) untouched — the token gate is
   bypassed for safe methods.

The exempt routes are registered explicitly in the middleware; here we
verify the contract from the test side.
"""

from tests.cockpit.helpers import csrf_post

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider


def _escalation_payload(user_id: str = "") -> dict:
    return {
        "id": "esc-csrf-1",
        "chat_id": "120363@g.us",
        "conversation_id": "120363@g.us",
        "reason": "csrf test",
        "severity": "high",
        "summary": "synthetic escalation for csrf tests",
        "user_id": user_id,
        "created_at": "2026-07-24T00:00:00+00:00",
        "resolved": False,
        "resolved_at": None,
        "resolved_by": None,
    }


def _login(client, db, user) -> None:
    tokens.create_login_request(db, user.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(user.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302


class TestCsrfTokenEndpoint:
    def test_get_csrf_returns_token(self, client):
        """The test-only token endpoint must always return a token (no
        session required) so the helper can prime it before any login."""
        resp = client.get("/_csrf")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["token"], str) and body["token"]


class TestCsrfEnforcement:
    def test_post_without_token_returns_403(self, client, db, user):
        """A session-authenticated POST without any CSRF token is rejected."""
        _login(client, db, user)
        resp = client.post("/connections/resend", follow_redirects=False)
        assert resp.status_code == 403

    def test_post_with_token_succeeds(self, client, db, user):
        """The same POST with a valid _csrf token succeeds (302 redirect)."""
        _login(client, db, user)
        resp = csrf_post(client, "/connections/resend", follow_redirects=False)
        assert resp.status_code == 302

    def test_post_with_wrong_token_returns_403(self, client, db, user):
        """A token that does not match the session's csrf_token is rejected."""
        _login(client, db, user)
        # Prime the cache so the helper is happy, then corrupt the form payload.
        resp = csrf_post(client, "/connections/resend", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        # Now send a raw POST with an explicitly wrong token.
        resp = client.post(
            "/connections/resend",
            data={"_csrf": "totally-wrong-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_post_with_token_via_header_succeeds(self, client, db, user):
        """The middleware accepts the token via ``X-CSRF-Token`` header too,
        which is how JSON-bodied POSTs (e.g. resolve) carry it."""
        _login(client, db, user)
        token_resp = client.get("/_csrf")
        token = token_resp.json()["token"]
        resp = client.post(
            "/connections/resend",
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 302


class TestWebhookExempt:
    def test_webhook_post_without_token_is_not_blocked(self, client):
        """``/webhook/...`` is machine-consumer (HMAC-verified), so CSRF
        does not apply — a missing token must not gate the route. The
        route may 404/401/413 etc. for its own reasons, but never 403-for-CSRF."""
        resp = client.post("/webhook/some-slug/resend", json={"text": "hi"})
        assert resp.status_code != 403


class TestEscalationIngestExempt:
    def test_escalation_ingest_without_token_is_not_blocked(self, client):
        """The bare ``/api/escalations`` ingest is bearer-authed once
        ``KAI_COCKPIT_ESCALATION_SECRET`` is set; in tests (no secret) it
        is unauthenticated by design. Either way, no CSRF gate."""
        resp = client.post("/api/escalations", json=_escalation_payload())
        # In test mode (no secret) the route accepts and returns 201.
        assert resp.status_code == 201

    def test_resolve_endpoint_is_csrf_protected(self, client, db, user):
        """But ``/api/escalations/{id}/resolve`` is session-authed and
        must be CSRF-protected like every other browser POST."""
        client.post("/api/escalations", json=_escalation_payload(user.kai_slug))
        _login(client, db, user)
        # Without a token → 403
        resp = client.post("/api/escalations/esc-csrf-1/resolve")
        assert resp.status_code == 403
        # With a token → success
        resp = csrf_post(client, "/api/escalations/esc-csrf-1/resolve")
        assert resp.json()["ok"] is True


class TestLogoutExempt:
    def test_logout_get_is_unaffected(self, client):
        """``/logout`` is a GET (safe method) and exempt from CSRF by
        definition. It must not 403 even though the client has no session
        cookie at all yet. (Anonymous /logout returns 302 to /login.)"""
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code != 403
