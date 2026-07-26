"""Tests for the deployment wizard: GET, POST, template picker."""

import secrets
from datetime import UTC, datetime

from tests.cockpit.helpers import csrf_post

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import Connection, User


def _login(client, db, bob):
    """Drive the login flow and return an authenticated client."""
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


def _create_user(db):
    """Create a user with connected email (resend + smtp) for wizard tests."""
    u = User(
        email="wizard@test.com",
        language="English",
        timezone="UTC",
        hmac_key=secrets.token_hex(32),
        created_at=datetime.now(UTC).isoformat(),
        kai_slug="wizard",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    resend = Connection(
        user_id=u.id,
        service="resend",
        status="connected",
        config={"signing_secret": "wizard-signing", "api_key": "re_wizard"},
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    smtp = Connection(
        user_id=u.id,
        service="smtp",
        status="connected",
        config={},
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    db.add(resend)
    db.add(smtp)
    db.commit()
    return u


def _post_deploy(
    client, bot_type="email", template="general", goal="test goal", language="English"
):
    return csrf_post(
        client,
        "/deployments/new",
        data={
            "bot_type": bot_type,
            "goal": goal,
            "language": language,
            "template": template,
        },
        follow_redirects=False,
    )


class TestWizardGET:
    def test_get_shows_templates(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = client.get("/deployments/new?bot_type=email")
        assert resp.status_code == 200
        # General should be one of the templates shown
        assert b"general" in resp.content


class TestWizardPOST:
    def test_post_creates_with_general_template(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = _post_deploy(client, template="general")
        assert resp.status_code == 302
        # Check deployment was created with general template
        from kai.cockpit.deployments import DeploymentsService

        svc = DeploymentsService(db)
        dep = svc.get_for_user_and_type(user.id, "email")
        assert dep is not None
        assert dep.template == "general"

    def test_post_creates_with_custom_template(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = _post_deploy(client, template="customer-support")
        assert resp.status_code == 302
        from kai.cockpit.deployments import DeploymentsService

        svc = DeploymentsService(db)
        dep = svc.get_for_user_and_type(user.id, "email")
        assert dep is not None
        assert dep.template == "customer-support"

    def test_post_rejects_nonexistent_template(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = _post_deploy(client, template="nonexistent_template_xyz")
        assert resp.status_code == 200  # re-renders wizard with error
        assert b"Invalid template" in resp.content or b"Could not create agent" in resp.content


class TestTemplatePreview:
    def test_preview_returns_200(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = client.get("/deployments/new/preview?bot_type=email&template=general")
        assert resp.status_code == 200
        # Should contain the template's display_name
        assert b"kAI" in resp.content

    def test_preview_shows_display_name(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = client.get("/deployments/new/preview?bot_type=email&template=customer-support")
        assert resp.status_code == 200
        assert b"kAI Support" in resp.content

    def test_preview_unknown_template_404(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        resp = client.get("/deployments/new/preview?bot_type=email&template=nonexistent_xyz")
        assert resp.status_code == 404
