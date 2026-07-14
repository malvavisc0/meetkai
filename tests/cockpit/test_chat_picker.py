"""Route-level tests for the chat picker endpoint (GET /deployments/{id}/chats.json).

Covers happy path, WAHA-unreachable (graceful empty 200), pagination flag,
unauthenticated redirect, and not-owner (silent empty) cases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import Connection, User
from kai.cockpit.naming import kai_slug_for
from kai.utils.common import now_iso


@pytest.fixture
def bob(db):
    u = User(
        email="bob@x.com",
        language="English",
        timezone="UTC",
        hmac_key="bob-hmac-key",
        created_at=datetime.now(UTC).isoformat(),
        kai_slug=kai_slug_for("bob@x.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def conn(db, bob):
    c = Connection(
        user_id=bob.id,
        service="whatsapp",
        status="connected",
        config={"waha_session": "bob-session", "waha_webhook_port": 8100},
        webhook_port=8100,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture
def dep(db, bob, conn):
    """Depends on ``conn`` — DeploymentsService.create() now requires a
    connected WhatsApp Connection to exist before a ``waha`` deployment can
    be created at all."""
    from kai.cockpit.deployments import DeploymentsService

    return DeploymentsService(db).create(bob, "waha", "be helpful", "English")


def _login(client, db, bob):
    tokens.create_login_request(db, bob.id)
    token = MagicLinkProvider(db).initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


def _mock_waha(monkeypatch, *, overview=None, raises=None):
    mock = AsyncMock()
    mock.close = AsyncMock()
    if raises is not None:
        mock.get_chats_overview = AsyncMock(side_effect=raises)
    else:
        mock.get_chats_overview = AsyncMock(return_value=overview or [])
    monkeypatch.setattr("kai.cockpit.routes.deployments.WahaClient", lambda settings: mock)
    return mock


class TestChatsJson:
    def test_happy_path(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        _mock_waha(
            monkeypatch,
            overview=[
                {"id": "120363@g.us", "name": "Kai Group", "picture": None},
                {"id": "591123@c.us", "name": "Maria", "picture": None},
                {"id": "555@lid", "name": None, "picture": None},
            ],
        )
        r = client.get(f"/deployments/{dep.id}/chats.json", params={"limit": 20, "offset": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["has_more"] is False
        assert len(body["chats"]) == 3
        assert body["chats"][0] == {"id": "120363@g.us", "name": "Kai Group", "avatar_initial": "K"}
        assert body["chats"][1] == {"id": "591123@c.us", "name": "Maria", "avatar_initial": "M"}
        # No name -> falls back to id's first char.
        assert body["chats"][2]["avatar_initial"] == "5"

    def test_has_more_when_page_full(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        # Over-fetch: route asks WAHA for limit+1; returning limit+1 rows
        # signals there's another page, and the response is trimmed to limit.
        _mock_waha(
            monkeypatch,
            overview=[{"id": f"{i}@c.us", "name": f"C{i}"} for i in range(3)],
        )
        r = client.get(f"/deployments/{dep.id}/chats.json", params={"limit": 2, "offset": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["has_more"] is True
        assert len(body["chats"]) == 2

    def test_has_more_false_on_last_page(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        # Fewer than limit+1 rows -> last page, no more.
        _mock_waha(
            monkeypatch,
            overview=[{"id": f"{i}@c.us", "name": f"C{i}"} for i in range(2)],
        )
        r = client.get(f"/deployments/{dep.id}/chats.json", params={"limit": 2, "offset": 0})
        assert r.status_code == 200
        assert r.json()["has_more"] is False

    def test_waha_failure_surfaces_error(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        _mock_waha(monkeypatch, raises=RuntimeError("waha down"))
        r = client.get(f"/deployments/{dep.id}/chats.json")
        # WAHA failures are surfaced to the user (with a pointer to
        # /dependencies) rather than silently rendering an empty picker or
        # propagating a raw 500 the user can't act on. The underlying
        # exception text (which can include internal WAHA host/URL details)
        # is logged server-side, not echoed back in the response.
        assert r.status_code == 200
        body = r.json()
        assert body["chats"] == []
        assert body["has_more"] is False
        assert body["error"] == "Could not load chats for this WhatsApp session"
        assert "waha down" not in body["error"]

    def test_client_construction_failure_surfaces_error(
        self, client, db, bob, conn, dep, monkeypatch
    ):
        # A failure building the WahaClient itself (e.g. bad/missing WAHA
        # settings) must degrade the same way as a failed chats/overview
        # call, not bubble up as an unhandled 500.
        _login(client, db, bob)

        def _boom(settings):
            raise RuntimeError("bad waha settings: hmac_key not configured")

        monkeypatch.setattr("kai.cockpit.routes.deployments.WahaClient", _boom)
        r = client.get(f"/deployments/{dep.id}/chats.json")
        assert r.status_code == 200
        body = r.json()
        assert body["chats"] == []
        assert body["has_more"] is False
        assert body["error"] == "Could not load chats for this WhatsApp session"
        assert "bad waha settings" not in body["error"]

    def test_no_connection_returns_empty(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        _mock_waha(monkeypatch, overview=[{"id": "1@c.us", "name": "x"}])
        # dep required conn to exist at creation time — remove it now to
        # simulate an operator who disconnected WhatsApp afterward.
        db.delete(conn)
        db.commit()
        r = client.get(f"/deployments/{dep.id}/chats.json")
        assert r.status_code == 200
        assert r.json()["chats"] == []

    def test_unauthenticated_redirects(self, client, db, bob, conn, dep):
        r = client.get(f"/deployments/{dep.id}/chats.json", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_not_owner_returns_empty(self, client, db, bob, conn, dep, monkeypatch):
        # A second user logs in and tries bob's deployment.
        alice = User(
            email="alice@x.com",
            language="French",
            timezone="UTC",
            hmac_key="alice-hmac-key",
            created_at=datetime.now(UTC).isoformat(),
            kai_slug=kai_slug_for("alice@x.com"),
        )
        db.add(alice)
        db.commit()
        _login(client, db, alice)
        _mock_waha(monkeypatch, overview=[{"id": "1@c.us", "name": "x"}])
        r = client.get(f"/deployments/{dep.id}/chats.json")
        assert r.status_code == 200
        assert r.json()["chats"] == []


class TestSettingsPageRender:
    def test_settings_page_renders_picker_markup(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        _mock_waha(monkeypatch, overview=[])
        r = client.get(f"/deployments/{dep.id}/settings")
        assert r.status_code == 200
        assert "data-chat-picker" in r.text
        assert 'id="chat_search"' in r.text
        assert 'id="chat_load_more"' in r.text
        # Triggers card must still be present (not removed by the swap).
        assert 'id="trigger_keyword"' in r.text
        assert 'name="mentions_enabled"' in r.text
        # Raw textareas remain as the POST source of truth.
        assert 'id="whitelist"' in r.text
        assert 'id="blacklist"' in r.text


class TestWhitelistBlacklistRoundTrip:
    """Lock the textarea parsing contract shared by the server POST handler
    (deployments.py) and the client JS (chatIdsFromTextarea): ids are
    newline-separated, trimmed, and blank lines are dropped. Drift between the
    two parsers would make a row the JS treats as selected not round-trip
    through the form submit.
    """

    def test_normalizes_whitespace_and_blank_lines(self, client, db, bob, conn, dep, monkeypatch):
        _login(client, db, bob)
        messy = "  120363@g.us  \n\n 591@c.us \n\n  \n 154@lid "
        r = client.post(
            f"/deployments/{dep.id}/settings",
            data={"goal": "be helpful", "language": "English", "whitelist": messy},
            follow_redirects=False,
        )
        assert r.status_code == 302

        db.refresh(dep)
        assert dep.settings["whitelist"] == ["120363@g.us", "591@c.us", "154@lid"]

        # The settings page renders the normalized value back into the textarea.
        page = client.get(f"/deployments/{dep.id}/settings")
        assert page.status_code == 200
        assert ">120363@g.us\n591@c.us\n154@lid</textarea>" in page.text
