"""Route-level tests for the feature-flag entitlement gate.

A deployment's feature_flags may only be enabled for flags the user is
entitled to. The settings form renders every flag the bot type declares,
marking unentitled ones disabled + unchecked; a direct POST can spoof
checkbox names — the server must clamp them server-side.
"""

from datetime import UTC, datetime

import pytest
from tests.cockpit.helpers import _connect_email, csrf_post

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import User
from kai.cockpit.naming import kai_slug_for


@pytest.fixture
def bob(db):
    u = User(
        email="bob@x.com",
        language="English",
        timezone="UTC",
        hmac_key="bob-hmac-key",
        feature_flags={"image": True, "sso": False},
        created_at=datetime.now(UTC).isoformat(),
        kai_slug=kai_slug_for("bob@x.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def dep(db, bob):
    from kai.cockpit.deployments import DeploymentsService
    from kai.cockpit.models import Connection

    _connect_email(db, bob)
    now = datetime.now(UTC).isoformat()
    db.add(
        Connection(
            user_id=bob.id,
            service="smtp",
            status="connected",
            config={
                "host": "smtp.example.com",
                "port": 587,
                "username": "u",
                "password": "p",
                "from_address": "a@b.c",
                "use_tls": True,
            },
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()

    svc = DeploymentsService(db)
    d = svc.create(bob, "email", "be helpful", "English")
    return d


def _login(client, db, bob):
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


class TestEntitlementGate:
    def test_entitled_flag_can_be_disabled(self, client, db, bob, dep):
        """An entitled flag can be turned off via the form."""
        _login(client, db, bob)
        csrf_post(
            client,
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "be helpful",
                "language": "English",
                # feature_image intentionally omitted -> False
            },
            follow_redirects=False,
        )
        db.refresh(dep)
        assert dep.feature_flags["image"] is False


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

        result = CliRunner().invoke(cockpit_user_app, ["flags", bob.email, "--sso"])
        assert result.exit_code == 0
        db.refresh(bob)
        assert bob.feature_flags["sso"] is True
        assert bob.feature_flags.get("image") is not True

    def test_flags_revoke(self, db, bob):
        from typer.testing import CliRunner

        from kai.cli import cockpit_user_app

        result = CliRunner().invoke(cockpit_user_app, ["flags", bob.email, "--no-image"])
        assert result.exit_code == 0
        db.refresh(bob)
        assert bob.feature_flags["image"] is False
