"""Phase 2 smoke tests — template-driven bot behavior wiring.

Verifies the Phase 2 success criteria:
- ``configure()`` receives the resolved template + ``ToolResolution`` and drives
  actions / post-processing / reply_style / tool gating from it.
- Bot-owned tool registration (``get_whatsapp_history``, conversation tools,
  the task scheduler) respects the resolved tool set — a template that omits a
  tool gets it absent.
- The ``general`` default reproduces the previous hardcoded behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kai.bots.email import Bot as EmailBot
from kai.bots.email.setup import BotConfig as EmailBotConfig
from kai.bots.waha import Bot as WahaBot
from kai.bots.waha.actions import (
    WahaAction,
    WahaNoSilentAction,
    WahaNoSilentNoVoiceAction,
    WahaNoVoiceAction,
    action_cls_for_turn,
)
from kai.bots.waha.setup import BotConfig as WahaBotConfig
from kai.config.settings import Settings
from kai.templates import TemplateRegistry
from kai.templates.resolver import resolve_tools
from kai.templates.schema import (
    PostProcessingConfig,
    TemplateDef,
    TemplateTools,
)


def _general(transport: str) -> TemplateDef:
    return TemplateRegistry.bundled().get(transport, "general")


def _waha_dir() -> Path:
    import kai.bots.waha as waha_mod

    return Path(waha_mod.__file__).resolve().parent


def _email_dir() -> Path:
    import kai.bots.email as email_mod

    return Path(email_mod.__file__).resolve().parent


def _fake_agent() -> MagicMock:
    """A MagicMock agent that records registered tool names + workflow calls."""
    agent = MagicMock()
    agent._registered: list[str] = []
    agent._workflows: list[str] = []

    def _register_tool(tool):
        agent._registered.append(tool.metadata.name)

    def _add_tool_workflow(workflow):
        if workflow is not None:
            agent._workflows.append(workflow)

    agent.register_tool.side_effect = _register_tool
    agent.add_tool_workflow.side_effect = _add_tool_workflow
    return agent


def _settings() -> Settings:
    return Settings.for_test(agent_history_folder=None)


@pytest.fixture
def _email_env(monkeypatch):
    """EmailSettings requires KAI_BOT_CONTROL_PORT + KAI_BOT_HMAC_KEY."""
    monkeypatch.setenv("KAI_BOT_CONTROL_PORT", "8721")
    monkeypatch.setenv("KAI_BOT_HMAC_KEY", "test-secret")


class TestWahaGeneralWiring:
    def test_general_drives_actions_reply_style_post_processing(self):
        bot = WahaBot(_waha_dir())
        agent = _fake_agent()
        tmpl = _general("waha")
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        assert bot._base_actions == tuple(tmpl.actions)
        assert bot._reply_style == tmpl.reply_style
        assert bot._post_processor._config.profile == "waha_default"

    def test_general_registers_bot_owned_tools(self):
        bot = WahaBot(_waha_dir())
        agent = _fake_agent()
        tmpl = _general("waha")
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        names = set(agent._registered)
        assert "get_whatsapp_history" in names
        assert "record_note" in names
        assert "get_conversation_messages" in names

    def test_general_injects_web_workflow(self):
        bot = WahaBot(_waha_dir())
        agent = _fake_agent()
        tmpl = _general("waha")
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        assert agent._workflows  # WEB_WORKFLOW_INSTRUCTIONS injected

    def test_disabling_web_search_omits_workflow(self):
        # The web-search workflow tracks tool presence, not a template flag:
        # an operator who disables web_search gets no usage guidance for tools
        # the bot no longer has.
        tmpl = _general("waha")
        bot = WahaBot(_waha_dir())
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], ["web_search"])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        assert "web_search" not in tools.final_tools
        assert not agent._workflows

    def test_action_cls_for_turn_uses_base_actions(self):
        # A template with a reduced action set (no send_voice_note) yields an
        # output_cls that never offers send_voice_note, even with TTS on.
        cls = action_cls_for_turn(
            base_actions=("reply", "silent", "console"),
            allow_silence=True,
            tts_available=True,
        )
        assert "send_voice_note" not in _action_values(cls)
        assert "reply" in _action_values(cls)

    def test_canonical_action_classes_are_singletons(self):
        # Identical action tuples return the same class object — the
        # memoization that keeps existing ``is WahaAction`` assertions green.
        from kai.bots.waha.actions import _FULL_ACTION_NAMES, build_action_cls

        assert build_action_cls(_FULL_ACTION_NAMES) is WahaAction
        assert build_action_cls(tuple(a for a in _FULL_ACTION_NAMES if a != "silent")) is (
            WahaNoSilentAction
        )
        assert (
            build_action_cls(tuple(a for a in _FULL_ACTION_NAMES if a != "send_voice_note"))
            is WahaNoVoiceAction
        )
        assert (
            build_action_cls(
                tuple(a for a in _FULL_ACTION_NAMES if a not in ("silent", "send_voice_note"))
            )
            is WahaNoSilentNoVoiceAction
        )


class TestToolGating:
    def test_template_omitting_task_tools_gets_no_scheduler(self):
        # A focused template that declares no task tools → setup_task_scheduler
        # skips wiring (no TaskScheduler, no schedule_task tool registered).
        tmpl = TemplateDef(
            name="focused",
            transport="waha",
            display_name="Focused",
            description="No tasks",
            actions=["reply", "silent"],
            tools=TemplateTools(),
        )
        bot = WahaBot(_waha_dir(), config=WahaBotConfig(trigger_keyword="kai"))
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        names = set(agent._registered)
        assert "schedule_task" not in names
        assert "list_tasks" not in names
        assert "cancel_task" not in names
        assert bot._task_scheduler is None

    def test_template_omitting_history_tool_skips_registration(self):
        tmpl = TemplateDef(
            name="nohistory",
            transport="waha",
            display_name="NoHistory",
            description="No history tool",
            actions=["reply", "silent"],
            tools=TemplateTools(),
        )
        bot = WahaBot(_waha_dir(), config=WahaBotConfig(trigger_keyword="kai"))
        agent = _fake_agent()
        tools = resolve_tools(tmpl, [], [])
        bot.configure(agent, _settings(), template=tmpl, tools=tools)

        assert "get_whatsapp_history" not in set(agent._registered)

    def test_phantom_enable_rejected(self):
        tmpl = TemplateDef(
            name="t",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        tools = resolve_tools(tmpl, ["barin_query"], [])
        assert "barin_query" in tools.rejected_unknown
        assert "barin_query" not in tools.final_tools


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
        # email has no transport-specific history tool
        assert "get_whatsapp_history" not in names

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


def _action_values(cls) -> set[str]:
    from kai.agent.core import _action_values

    return set(_action_values(cls))
