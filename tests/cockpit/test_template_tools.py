"""Tests for template tool overrides on the settings page."""

import secrets
from datetime import UTC, datetime

from tests.cockpit.helpers import csrf_post

from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.deployments import DeploymentsService
from kai.cockpit.models import Connection, User


def _login(client, db, bob):
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


def _create_user(db):
    u = User(
        email="settings_test@test.com",
        language="English",
        timezone="UTC",
        hmac_key=secrets.token_hex(32),
        created_at=datetime.now(UTC).isoformat(),
        kai_slug="settings_test",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    resend = Connection(
        user_id=u.id,
        service="resend",
        status="connected",
        config={"signing_secret": "test-signing", "api_key": "re_test"},
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


def _create_and_get_dep(db, user):
    svc = DeploymentsService(db)
    dep = svc.create(user, "email", "test goal", "English", template="general")
    return dep


class TestSettingsTemplateTools:
    def test_get_has_template_tools_partial_included(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        _create_and_get_dep(db, user)
        resp = client.get("/deployments/1/settings")
        assert resp.status_code == 200
        # The partial includes checkbox inputs for template tools
        assert b"tool_override_" in resp.content


class TestSettingsToolOverridePersistence:
    def test_save_persists_tool_overrides(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        # POST with a tool override enable
        resp = csrf_post(
            client,
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "tool_override_web_search": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        svc = DeploymentsService(db)
        svc.db.refresh(dep)
        # web_search is optional and was checked — leave it as default (on).
        # It should NOT be in the disable list.
        assert "web_search" not in dep.tool_overrides.get("disable", [])

    def test_save_on_stopped_dep_no_restart_needed(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        resp = csrf_post(
            client,
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "tool_override_web_search": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        svc = DeploymentsService(db)
        svc.db.refresh(dep)
        assert dep.needs_restart is False

    def test_save_on_running_dep_sets_restart_needed(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)
        dep.status = "running"
        dep.desired_state = "running"
        db.commit()

        resp = csrf_post(
            client,
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "tool_override_web_search": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        svc = DeploymentsService(db)
        svc.db.refresh(dep)
        assert dep.needs_restart is True

    def test_disable_optional_tool(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        # General has web_search as an optional tool; unchecking it should
        # add it to the disable list.
        resp = csrf_post(
            client,
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        svc = DeploymentsService(db)
        svc.db.refresh(dep)
        # The tool was NOT checked, so it should be in the disable list
        # (since it's an optional tool in the general template)
        disable_list = dep.tool_overrides.get("disable", [])
        assert "web_search" in disable_list

    def test_disabled_optional_tool_reflected_on_get(self, client, db):
        """A persisted disable must render as unchecked on the next GET."""
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        # Persist a disabled web_search directly.
        svc = DeploymentsService(db)
        svc.edit(dep, tool_overrides={"enable": [], "disable": ["web_search"]})

        resp = client.get(f"/deployments/{dep.id}/settings")
        assert resp.status_code == 200
        # The web_search checkbox must NOT be checked.
        import re

        match = re.search(rb'name="tool_override_web_search"[^>]*>', resp.content)
        assert match is not None, "web_search checkbox missing from settings page"
        assert b"checked" not in match.group(0), "disabled tool rendered as checked"

    def test_reject_disable_required_tool(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        # Attempting to disable a tool that resolve_tools considers required
        # (a default tool) should be rejected.
        resp = csrf_post(
            client,
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "tool_override_escalate": "false",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
