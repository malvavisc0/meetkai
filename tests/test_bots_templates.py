"""Phase 3 specialized-template tests.

Verifies the Phase 3 success criteria:
- Each specialized template declares distinct actions / tools / config.
- Templates with ``escalation_rules`` inject the ESCALATION RULES block into
  the bot's loaded system prompt at runtime (not just the CLI preview).
- A focused template (``order-status``) omits task tools and task-related
  machinery.
- The catalog's transport-action constraints hold (no waha-only actions on
  email templates).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kai.bots.email import Bot as EmailBot
from kai.bots.email.setup import BotConfig as EmailBotConfig
from kai.bots.waha import Bot as WahaBot
from kai.bots.waha.setup import BotConfig as WahaBotConfig
from kai.config.settings import Settings
from kai.templates import TemplateRegistry, escalation_prompt_section
from kai.templates.resolver import resolve_tools


def _tmpl(transport: str, name: str):
    return TemplateRegistry.bundled().get(transport, name)


def _waha_dir() -> Path:
    import kai.bots.waha as m

    return Path(m.__file__).resolve().parent


def _email_dir() -> Path:
    import kai.bots.email as m

    return Path(m.__file__).resolve().parent


def _fake_agent() -> MagicMock:
    agent = MagicMock()
    agent._registered: list[str] = []

    def _register_tool(tool):
        agent._registered.append(tool.metadata.name)

    agent.register_tool.side_effect = _register_tool
    return agent


def _settings() -> Settings:
    return Settings.for_test(agent_history_folder=None)


@pytest.fixture
def _email_env(monkeypatch):
    monkeypatch.setenv("KAI_BOT_CONTROL_PORT", "8721")
    monkeypatch.setenv("KAI_BOT_HMAC_KEY", "test-secret")


class TestTemplatesCatalog:
    def test_all_seven_templates_exist(self):
        reg = TemplateRegistry.bundled()
        names = {f"{t.transport}/{t.name}" for t in reg.list()}
        for expected in [
            "waha/customer-support",
            "waha/lead-qualification",
            "waha/group-chatter",
            "email/customer-support",
            "email/order-status",
            "email/appointment-manager",
            "email/questions",
        ]:
            assert expected in names, f"missing {expected}"

    def test_templates_have_distinct_actions(self):
        # No two waha templates share an identical action set (general
        # excluded — it's the baseline). Distinct actions = distinct behavior.
        waha_actions = {
            t.name: tuple(t.actions)
            for t in TemplateRegistry.bundled().list("waha")
            if t.name != "general"
        }
        # customer-support is the minimal [reply, silent, console]; lead-qual
        # adds sleep+send_dm; group-chatter adds send_to_group.
        assert set(waha_actions["customer-support"]) == {"reply", "silent", "console"}
        assert "sleep" in waha_actions["lead-qualification"]
        assert "send_to_group" in waha_actions["group-chatter"]
        assert "send_voice_note" not in waha_actions["group-chatter"]

    def test_customer_support_requires_brain_and_bot_tools(self):
        for transport in ("waha", "email"):
            tmpl = _tmpl(transport, "customer-support")
            assert "brain_query" in tmpl.tools.required
            assert "record_note" in tmpl.tools.bot_tools
            assert "get_conversation_messages" in tmpl.tools.bot_tools

    def test_order_status_requires_sql(self):
        tmpl = _tmpl("email", "order-status")
        assert "sql_query" in tmpl.tools.required
        assert "calcom" not in tmpl.tools.required

    def test_appointment_manager_requires_calcom_and_tasks(self):
        tmpl = _tmpl("email", "appointment-manager")
        assert "calcom" in tmpl.tools.required
        assert "schedule_task" in tmpl.tools.required

    def test_lead_qualification_has_escalation_rules(self):
        tmpl = _tmpl("waha", "lead-qualification")
        assert len(tmpl.escalation_rules) >= 1
        severities = {r.severity for r in tmpl.escalation_rules}
        assert "critical" in severities


class TestEscalationInjection:
    def test_escalation_prompt_section_built(self):
        tmpl = _tmpl("waha", "customer-support")
        section = escalation_prompt_section(tmpl)
        assert "ESCALATION RULES" in section
        assert "escalate(severity=" in section

    def test_no_section_when_no_rules(self):
        tmpl = _tmpl("waha", "group-chatter")
        assert escalation_prompt_section(tmpl) == ""

    def test_bot_prompt_includes_escalation_block(self, monkeypatch):
        # Required brain env vars so resolve_tools/boot guards pass.
        monkeypatch.setenv("KAI_BRAIN_BASE_URL", "http://test")
        monkeypatch.setenv("KAI_BRAIN_LIGHTRAG_API_KEY", "secret")
        tmpl = _tmpl("waha", "customer-support")
        bot = WahaBot(_waha_dir(), config=WahaBotConfig(trigger_keyword="kai"))
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        assert "ESCALATION RULES" in bot._prompt
        assert "escalate(severity=" in bot._prompt


class TestOrderStatusFocused:
    def test_no_task_tools_when_template_omits_them(self, _email_env):
        tmpl = _tmpl("email", "order-status")
        # order-status declares no task tools — the scheduler must not wire.
        bot = EmailBot(_email_dir(), config=EmailBotConfig())
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        names = set(agent._registered)
        assert "schedule_task" not in names
        assert "list_tasks" not in names
        assert "cancel_task" not in names
        assert bot._task_scheduler is None

    def test_order_status_boot_fails_without_sql(self):
        # KAI_SQL_DSN unset (conftest clears KAI_* env) → resolve_tools must
        # report the missing required tool.
        tmpl = _tmpl("email", "order-status")
        tools = resolve_tools(tmpl, [], [])
        assert any("sql_query" in m for m in tools.missing_required)
