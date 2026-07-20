"""Unit tests for ``attention_reason`` — pure function, no DB required.

Pins down bot-type awareness: only a deployment that actually depends on
WhatsApp can be flagged "WhatsApp down, wants running". The ``email`` bot
declares ``required_connections=["resend", "smtp"]`` and must never
inherit a WhatsApp attention reason.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kai.cockpit.bots import BOT_TYPES
from kai.cockpit.deployments import attention_reason
from kai.cockpit.models import Deployment


def _dep(
    bot_type: str,
    *,
    status: str = "running",
    desired_state: str = "running",
    needs_restart: bool = False,
) -> Deployment:
    return Deployment(
        user_id=1,
        bot_type=bot_type,
        status=status,
        desired_state=desired_state,
        voice="af_heart",
        goal="help",
        language="English",
        needs_restart=needs_restart,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


class TestAttentionReason:
    def test_email_ignores_whatsapp_down(self):
        # Process healthy (status_data present, not None) — only the WhatsApp
        # branch is in question here. Email doesn't depend on WhatsApp, so the
        # WhatsApp-down condition must never surface on it.
        dep = _dep("email")
        assert attention_reason(dep, {"connected": False}, whatsapp_connected=False) is None
        assert attention_reason(dep, {"connected": True}, whatsapp_connected=False) is None
        assert attention_reason(dep, {"connected": True}, whatsapp_connected=True) is None

    def test_email_stopped_ignores_whatsapp_down(self):
        dep = _dep("email", status="stopped", desired_state="stopped")
        assert attention_reason(dep, None, whatsapp_connected=False) is None

    def test_waha_whatsapp_down_db_flag(self):
        dep = _dep("waha")
        assert (
            attention_reason(dep, None, whatsapp_connected=False) == "WhatsApp down, wants running"
        )

    def test_waha_whatsapp_down_live_status(self):
        dep = _dep("waha")
        assert (
            attention_reason(dep, {"connected": False}, whatsapp_connected=True)
            == "WhatsApp down, wants running"
        )

    def test_waha_whatsapp_connected_no_attention(self):
        dep = _dep("waha")
        assert attention_reason(dep, {"connected": True}, whatsapp_connected=True) is None

    def test_waha_process_not_responding(self):
        dep = _dep("waha", desired_state="stopped", status="running")
        assert (
            attention_reason(dep, None, whatsapp_connected=True) == "Bot process isn't responding"
        )

    def test_waha_needs_restart_when_connected(self):
        dep = _dep("waha", needs_restart=True)
        assert (
            attention_reason(dep, {"connected": True}, whatsapp_connected=True)
            == "Restart needed to apply settings"
        )

    def test_waha_needs_restart_yields_to_whatsapp_down(self):
        dep = _dep("waha", needs_restart=True)
        assert (
            attention_reason(dep, {"connected": False}, whatsapp_connected=False)
            == "WhatsApp down, wants running"
        )

    def test_stopped_deployment_no_attention(self):
        dep = _dep("waha", status="stopped", desired_state="stopped")
        assert attention_reason(dep, None, whatsapp_connected=False) is None

    def test_registry_email_not_whatsapp(self):
        assert "whatsapp" not in BOT_TYPES["email"].required_connections
        assert "whatsapp" in BOT_TYPES["waha"].required_connections
