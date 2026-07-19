"""Tests for template tool overrides on the settings page."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

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
    conn = Connection(
        user_id=u.id,
        service="whatsapp",
        status="connected",
        config={
            "waha_session": "settings-test-session",
            "waha_webhook_port": 8101,
            "waha_webhook_path": "/webhook/whatsapp-1",
        },
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return u


def _create_and_get_dep(db, user):
    svc = DeploymentsService(db)
    dep = svc.create(user, "waha", "test goal", "English", template="general")
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
        resp = client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "voice": "",
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

        resp = client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "voice": "",
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

        resp = client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "voice": "",
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

        # General has brain_query as an optional tool; unchecking it should
        # add it to the disable list.
        resp = client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "voice": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        svc = DeploymentsService(db)
        svc.db.refresh(dep)
        # The tool was NOT checked, so it should be in the disable list
        # (since it's an optional tool in the general template)
        disable_list = dep.tool_overrides.get("disable", [])
        assert "brain_query" in disable_list

    def test_disabled_optional_tool_reflected_on_get(self, client, db):
        """A persisted disable must render as unchecked on the next GET."""
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        # Persist a disabled brain_query directly.
        svc = DeploymentsService(db)
        svc.edit(dep, tool_overrides={"enable": [], "disable": ["brain_query"]})

        resp = client.get(f"/deployments/{dep.id}/settings")
        assert resp.status_code == 200
        # The brain_query checkbox must NOT be checked.
        import re

        match = re.search(rb'name="tool_override_brain_query"[^>]*>', resp.content)
        assert match is not None, "brain_query checkbox missing from settings page"
        assert b"checked" not in match.group(0), "disabled tool rendered as checked"

    def test_reject_disable_required_tool(self, client, db):
        user = _create_user(db)
        _login(client, db, user)
        dep = _create_and_get_dep(db, user)

        # Attempting to disable a tool that resolve_tools considers required
        # (a default tool) should be rejected.
        resp = client.post(
            f"/deployments/{dep.id}/settings",
            data={
                "goal": "test",
                "language": "English",
                "timezone": "",
                "brain_mandatory": "",
                "brain_instruction": "",
                "voice": "",
                "tool_override_escalate": "false",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
