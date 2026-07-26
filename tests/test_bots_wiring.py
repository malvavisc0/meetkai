"""Smoke tests — template-driven bot behavior wiring."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kai.bots.email import Bot as EmailBot
from kai.bots.email.setup import BotConfig as EmailBotConfig
from kai.templates import TemplateRegistry
from kai.templates.resolver import resolve_tools
from kai.templates.schema import (
    PostProcessingConfig,
    TemplateDef,
    TemplateTools,
)
from tests.conftest import make_test_settings as _settings


def _general(transport: str) -> TemplateDef:
    return TemplateRegistry.bundled().get(transport, "general")


def _email_dir() -> Path:
    import kai.bots.email as email_mod

    return Path(email_mod.__file__).resolve().parent


def _fake_agent() -> MagicMock:
    """A MagicMock agent that records registered tool names + workflow calls."""
    agent = MagicMock()
    agent._registered = []
    agent._workflows = []

    def _register_tool(tool):
        agent._registered.append(tool.metadata.name)

    def _add_tool_workflow(workflow):
        if workflow is not None:
            agent._workflows.append(workflow)

    agent.register_tool.side_effect = _register_tool
    agent.add_tool_workflow.side_effect = _add_tool_workflow
    return agent


@pytest.fixture
def _email_env(monkeypatch):
    """EmailSettings requires KAI_BOT_CONTROL_PORT + KAI_BOT_HMAC_KEY."""
    monkeypatch.setenv("KAI_BOT_CONTROL_PORT", "8721")
    monkeypatch.setenv("KAI_BOT_HMAC_KEY", "test-secret")


class TestEmailWiring:
    def test_general_drives_reply_style_post_processing(self, _email_env):
        bot = EmailBot(_email_dir())
        agent = _fake_agent()
        tmpl = _general("email")
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        assert bot._reply_style == tmpl.reply_style
        assert bot._post_processor._config.profile == "none"

    def test_general_registers_conversation_tools(self, _email_env):
        bot = EmailBot(_email_dir())
        agent = _fake_agent()
        tmpl = _general("email")
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        names = set(agent._registered)
        assert "record_note" in names
        assert "get_conversation_messages" in names

    def test_send_reply_applies_post_processor(self, _email_env):
        # A custom-profile template transforms reply text before SMTP send.
        # ``general`` uses profile=none (identity), so this asserts the wiring
        # end-to-end with a profile that actually changes the text.
        tmpl = _general("email").model_copy(
            update={"post_processing": PostProcessingConfig(profile="custom", strip_markdown=True)}
        )
        bot = EmailBot(_email_dir(), config=EmailBotConfig())
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)
        assert bot._post_processor.process("**hi**") == "hi"

    def test_template_omitting_task_tools_gets_no_scheduler(self, _email_env):
        # A focused template that declares no task tools → setup_task_scheduler
        # skips wiring (no TaskScheduler, no schedule_task tool registered).
        tmpl = TemplateDef(
            name="focused",
            transport="email",
            display_name="Focused",
            description="No tasks",
            actions=["reply", "silent"],
            tools=TemplateTools(),
        )
        bot = EmailBot(_email_dir(), config=EmailBotConfig())
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        names = set(agent._registered)
        assert "schedule_task" not in names
        assert "list_tasks" not in names
        assert "cancel_task" not in names
        assert bot._task_scheduler is None

    def test_phantom_enable_rejected(self):
        tmpl = TemplateDef(
            name="t",
            transport="email",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        tools = resolve_tools(tmpl, ["barin_query"], [])
        assert "barin_query" in tools.rejected_unknown
        assert "barin_query" not in tools.final_tools
