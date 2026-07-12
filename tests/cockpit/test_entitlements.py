"""Route-level tests for the feature-flag entitlement gate.

A deployment's feature_flags may only be enabled for flags the user is
entitled to. The settings form renders only entitled flags, but a direct
POST can spoof checkbox names — the server must clamp them server-side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import User


@pytest.fixture
def bob(db):
    u = User(
        email="bob@x.com",
        language="English",
        timezone="UTC",
        hmac_key="bob-hmac-key",
        feature_flags={"image": True, "video": False, "stt": False, "tts": False, "sso": False},
        created_at=datetime.now(UTC).isoformat(),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def fake_waha_client(monkeypatch):
    client = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr("kai.cockpit.connections.WahaClient", lambda settings: client)
    monkeypatch.setattr("kai.cockpit.connections.get_waha_settings", lambda: object())
    return client


@pytest.fixture
def dep(db, bob):
    from kai.cockpit.deployments import DeploymentsService
    from kai.cockpit.models import Connection

    db.add(
        Connection(
            user_id=bob.id,
            service="whatsapp",
            status="connected",
            config={
                "waha_session": "kai-bob",
                "waha_webhook_port": 8101,
                "waha_webhook_path": "/webhook/whatsapp-1",
            },
            created_at="now",
            updated_at="now",
        )
    )
    db.commit()

    svc = DeploymentsService(db)
    d = svc.create(bob, "waha", "be helpful", "English")
    return d


def _login(client, db, bob):
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


class TestEntitlementGate:
    def test_unentitled_flag_silently_dropped(self, client, db, bob, dep, fake_waha_client):
        """A direct POST with feature_video=on when the user lacks the video
        entitlement must NOT enable video on the deployment."""
        _login(client, db, bob)
        # video is NOT in bob's entitlements (False). Spoof it on.
        client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "be helpful",
                "language": "English",
                "voice": "af_heart",
                "feature_image": "true",
                "feature_video": "true",  # spoofed — should be clamped to False
                "feature_stt": "true",  # spoofed — should be clamped to False
            },
            follow_redirects=False,
        )
        db.refresh(dep)
        assert dep.feature_flags["image"] is True  # entitled -> kept
        assert dep.feature_flags["video"] is False  # not entitled -> dropped
        assert dep.feature_flags["stt"] is False  # not entitled -> dropped

    def test_entitled_flag_can_be_disabled(self, client, db, bob, dep, fake_waha_client):
        """An entitled flag can be turned off via the form."""
        _login(client, db, bob)
        client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "be helpful",
                "language": "English",
                "voice": "af_heart",
                # feature_image intentionally omitted -> False
            },
            follow_redirects=False,
        )
        db.refresh(dep)
        assert dep.feature_flags["image"] is False

    def test_settings_page_only_renders_entitled_flags(
        self, client, db, bob, dep, fake_waha_client
    ):
        """The settings form must not show flags the user can't enable."""
        _login(client, db, bob)
        r = client.get(f"/deployments/{dep.id}/settings")
        assert r.status_code == 200
        # bob is entitled to image only (among the waha flags).
        body = r.text
        assert "feature_image" in body
        assert "feature_video" not in body
        assert "feature_stt" not in body
        assert "feature_tts" not in body


class TestUserFlagsCli:
    def test_flags_show_defaults_off(self, db, bob):
        from typer.testing import CliRunner

        from kai.cli import cockpit_user_app

        # Re-derive flags from the DB row (bob has image=True).
        result = CliRunner().invoke(cockpit_user_app, ["flags", bob.email, "--show"])
        assert result.exit_code == 0
        assert "image" in result.output
        assert "on" in result.output

    def test_flags_toggle(self, db, bob):
        from typer.testing import CliRunner

        from kai.cli import cockpit_user_app

        bob.feature_flags = {}
        db.commit()

        result = CliRunner().invoke(cockpit_user_app, ["flags", bob.email, "--video", "--tts"])
        assert result.exit_code == 0
        db.refresh(bob)
        assert bob.feature_flags["video"] is True
        assert bob.feature_flags["tts"] is True
        assert bob.feature_flags.get("image") is not True

    def test_flags_revoke(self, db, bob):
        from typer.testing import CliRunner

        from kai.cli import cockpit_user_app

        result = CliRunner().invoke(cockpit_user_app, ["flags", bob.email, "--no-image"])
        assert result.exit_code == 0
        db.refresh(bob)
        assert bob.feature_flags["image"] is False
