"""Unit tests for ``attention_reason`` — pure function, no DB required.

Pins down the two attention triggers on a running deployment: a live
``/status`` probe that comes back empty (process died) and unapplied
settings changes (``needs_restart``). Stopped deployments never need
attention.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
        goal="help",
        language="English",
        needs_restart=needs_restart,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


class TestAttentionReason:
    def test_process_not_responding(self):
        dep = _dep("email")
        assert attention_reason(dep, None) == "Bot process isn't responding"

    def test_needs_restart(self):
        dep = _dep("email", needs_restart=True)
        assert attention_reason(dep, {"connected": True}) == "Restart needed to apply settings"

    def test_running_healthy_no_attention(self):
        dep = _dep("email")
        assert attention_reason(dep, {"connected": True}) is None

    def test_stopped_deployment_no_attention(self):
        dep = _dep("email", status="stopped", desired_state="stopped")
        assert attention_reason(dep, None) is None

    def test_email_needs_restart_yields_to_process_down(self):
        # When the process is down (status_data None), that verdict wins
        # over needs_restart.
        dep = _dep("email", needs_restart=True)
        assert attention_reason(dep, None) == "Bot process isn't responding"
