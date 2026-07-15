"""Tests for the console chat send route's "sent as real email" confirmation.

``POST /deployments/{id}/chat`` only shows a "sent as a real email to X"
confirmation when the bot's own ``/tell`` response confirms it dispatched a
real send via a ``send_reply`` action entry (see ``kai.bots.email.Bot.
handle_operator``) — a bot process still running pre-``to``-aware code
silently drops the field and returns no such entry, so the console must not
claim success in that case.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import Deployment, User
from kai.cockpit.naming import kai_slug_for


@pytest.fixture
def bob(db):
    u = User(
        email="bob@x.com",
        language="English",
        timezone="UTC",
        hmac_key="bob-hmac-key",
        created_at="now",
        is_disabled=False,
        kai_slug=kai_slug_for("bob@x.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def email_dep(db, bob):
    dep = Deployment(
        user_id=bob.id,
        bot_type="email",
        status="running",
        desired_state="running",
        voice="",
        goal="support customers",
        language="English",
        created_at="now",
        updated_at="now",
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return dep


def _login(client, db, bob):
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


class TestChatSendConfirmation:
    def test_shows_confirmation_when_bot_confirms_send(
        self, client, db, bob, email_dep, monkeypatch
    ):
        import kai.cockpit.deployments as dep_mod

        monkeypatch.setattr(
            dep_mod.DeploymentsService, "fetch_status", MagicMock(return_value={"bot": "email"})
        )
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "send_message",
            MagicMock(
                return_value={
                    "ok": True,
                    "reply": "answered",
                    "actions": [{"tool": "send_reply", "target": "alice@example.com", "ok": True}],
                }
            ),
        )
        _login(client, db, bob)
        r = client.post(
            f"/deployments/{email_dep.id}/chat",
            data={"message": "hi", "to": "alice@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        r2 = client.get(f"/deployments/{email_dep.id}")
        assert "Sent as a real email to" in r2.text
        assert "alice@example.com" in r2.text

    def test_no_confirmation_when_bot_does_not_confirm(
        self, client, db, bob, email_dep, monkeypatch
    ):
        """A stale bot process that ignores ``to`` returns no ``actions``
        entry — the console must not claim a send happened."""
        import kai.cockpit.deployments as dep_mod

        monkeypatch.setattr(
            dep_mod.DeploymentsService, "fetch_status", MagicMock(return_value={"bot": "email"})
        )
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "send_message",
            MagicMock(return_value={"ok": True, "reply": "answered"}),
        )
        _login(client, db, bob)
        r = client.post(
            f"/deployments/{email_dep.id}/chat",
            data={"message": "hi", "to": "alice@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        r2 = client.get(f"/deployments/{email_dep.id}")
        assert "Sent as a real email to" not in r2.text

    def test_no_confirmation_when_to_omitted(self, client, db, bob, email_dep, monkeypatch):
        import kai.cockpit.deployments as dep_mod

        monkeypatch.setattr(
            dep_mod.DeploymentsService, "fetch_status", MagicMock(return_value={"bot": "email"})
        )
        monkeypatch.setattr(
            dep_mod.DeploymentsService,
            "send_message",
            MagicMock(return_value={"ok": True, "reply": "local only"}),
        )
        _login(client, db, bob)
        r = client.post(
            f"/deployments/{email_dep.id}/chat",
            data={"message": "hi"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        r2 = client.get(f"/deployments/{email_dep.id}")
        assert "Sent as a real email to" not in r2.text
