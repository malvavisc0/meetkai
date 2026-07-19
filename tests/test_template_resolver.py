from typing import get_args

from kai.bots.email import EmailAction
from kai.bots.email.setup import BotConfig as EmailBotConfig
from kai.bots.waha.actions import _FULL_ACTION_NAMES
from kai.bots.waha.setup import BotConfig as WahaBotConfig
from kai.templates.resolver import (
    _EMAIL_VALID_ACTIONS,
    _WAHA_VALID_ACTIONS,
    resolve_config,
    resolve_tools,
    validate_actions,
    validate_tools,
)
from kai.templates.schema import (
    TemplateDef,
    TemplateTools,
)


class TestResolveTools:
    def test_defaults_only(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        result = resolve_tools(tmpl, [], [])
        assert "web_search" in result.final_tools
        assert "calculate" in result.final_tools
        assert not result.missing_required

    def test_required_tools_added(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["schedule_task"]),
        )
        result = resolve_tools(tmpl, [], [])
        assert "schedule_task" in result.final_tools

    def test_optional_respects_env(self, monkeypatch):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(optional=["brain_query"]),
        )
        monkeypatch.delenv("KAI_BRAIN_BASE_URL", raising=False)
        monkeypatch.delenv("KAI_BRAIN_MORPHIK_TOKEN", raising=False)
        result = resolve_tools(tmpl, [], [])
        assert "brain_query" not in result.final_tools

    def test_optional_added_when_env_present(self, monkeypatch):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(optional=["brain_query"]),
        )
        # Brain needs BOTH base_url AND morphik_api_key (mirrors brain_enabled).
        monkeypatch.setenv("KAI_BRAIN_BASE_URL", "http://test")
        monkeypatch.setenv("KAI_BRAIN_MORPHIK_TOKEN", "secret")
        result = resolve_tools(tmpl, [], [])
        assert "brain_query" in result.final_tools

    def test_operator_enable_adds_tool(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        result = resolve_tools(tmpl, ["sql_query"], [])
        assert "sql_query" in result.final_tools

    def test_operator_disable_removes_optional(self, monkeypatch):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(optional=["brain_query"]),
        )
        monkeypatch.setenv("KAI_BRAIN_BASE_URL", "http://test")
        monkeypatch.setenv("KAI_BRAIN_MORPHIK_TOKEN", "secret")
        # sanity: brain_query would be added without the disable
        assert "brain_query" in resolve_tools(tmpl, [], []).final_tools
        result = resolve_tools(tmpl, [], ["brain_query"])
        assert "brain_query" not in result.final_tools

    def test_can_disable_default_tool(self):
        # Default tools other than the safety trio (escalate,
        # blacklist, calculate) are disableable so a focused template can shed
        # web_search / get_weather etc.
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        result = resolve_tools(tmpl, [], ["web_search"])
        assert not result.rejected_disable
        assert "web_search" not in result.final_tools

    def test_cannot_disable_safety_tool(self):
        # escalate / blacklist / calculate are non-disableable safety defaults.
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        for tool in ("escalate", "blacklist", "calculate"):
            result = resolve_tools(tmpl, [], [tool])
            assert result.rejected_disable
            assert tool in result.final_tools

    def test_cannot_disable_required_tool(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["schedule_task"]),
        )
        result = resolve_tools(tmpl, [], ["schedule_task"])
        assert result.rejected_disable
        assert "schedule_task" in result.final_tools

    def test_missing_required_reported(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["brain_query"]),
        )
        result = resolve_tools(tmpl, [], [])
        assert "brain_query" in result.missing_required[0]

    def test_missing_required_reported_with_env(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["sql_query"]),
        )
        result = resolve_tools(tmpl, [], [])
        assert "KAI_SQL_DSN" in result.missing_required[0]


class TestValidateActions:
    def test_valid_waha_actions(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply", "send_voice_note", "silent"],
        )
        assert validate_actions(tmpl) == []

    def test_valid_email_actions(self):
        tmpl = TemplateDef(
            name="test",
            transport="email",
            display_name="T",
            description="T",
            actions=["reply", "console", "silent"],
        )
        assert validate_actions(tmpl) == []

    def test_invalid_waha_action_in_email(self):
        tmpl = TemplateDef(
            name="test",
            transport="email",
            display_name="T",
            description="T",
            actions=["reply", "send_voice_note"],
        )
        errors = validate_actions(tmpl)
        assert len(errors) == 1
        assert "send_voice_note" in errors[0]

    def test_invalid_action(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply", "misty"],
        )
        errors = validate_actions(tmpl)
        assert len(errors) == 1
        assert "misty" in errors[0]


class TestResolveConfig:
    def test_waha_template_defaults(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            config={"temperature": 0.5},
        )
        result = resolve_config(tmpl, None, {}, WahaBotConfig)
        assert result.temperature == 0.5

    def test_cli_overrides_template(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            config={"temperature": 0.5},
        )
        result = resolve_config(tmpl, None, {"temperature": 0.8}, WahaBotConfig)
        assert result.temperature == 0.8

    def test_config_file_overrides_template(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            config={"temperature": 0.5},
        )
        result = resolve_config(
            tmpl,
            {"temperature": 0.6},
            {},
            WahaBotConfig,
        )
        assert result.temperature == 0.6

    def test_cli_overrides_config_file(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            config={"temperature": 0.5},
        )
        result = resolve_config(
            tmpl,
            {"temperature": 0.6},
            {"temperature": 0.8},
            WahaBotConfig,
        )
        assert result.temperature == 0.8

    def test_email_config(self):
        tmpl = TemplateDef(
            name="test",
            transport="email",
            display_name="T",
            description="T",
            actions=["reply"],
            config={"language": "Spanish"},
        )
        result = resolve_config(tmpl, None, {}, EmailBotConfig)
        assert result.language == "Spanish"

    def test_nested_merge(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            config={
                "participation": {
                    "rate": 0.5,
                    "cooldown_seconds": 60,
                }
            },
        )
        result = resolve_config(tmpl, None, {}, WahaBotConfig)
        assert result.participation.rate == 0.5
        assert result.participation.cooldown_seconds == 60

    def test_nested_partial_override(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            config={
                "participation": {
                    "rate": 0.5,
                }
            },
        )
        result = resolve_config(tmpl, None, {}, WahaBotConfig)
        assert result.participation.rate == 0.5
        assert result.participation.enabled is True


class TestValidateTools:
    def test_no_missing_when_required_empty(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(),
        )
        assert validate_tools(tmpl) == []

    def test_missing_required_tool(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["sql_query"]),
        )
        missing = validate_tools(tmpl)
        assert any("sql_query" in m for m in missing)

    def test_not_missing_when_env_present(self, monkeypatch):
        monkeypatch.setenv("KAI_SQL_DSN", "sqlite:///:memory:")
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["sql_query"]),
        )
        assert validate_tools(tmpl) == []

    def test_brain_requires_both_env_vars(self, monkeypatch):
        # brain_enabled = base_url AND morphik_api_key — only one is not enough.
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["brain_query"]),
        )
        monkeypatch.delenv("KAI_BRAIN_BASE_URL", raising=False)
        monkeypatch.delenv("KAI_BRAIN_MORPHIK_TOKEN", raising=False)
        monkeypatch.setenv("KAI_BRAIN_BASE_URL", "http://test")
        assert validate_tools(tmpl)  # still missing — needs the key too
        monkeypatch.setenv("KAI_BRAIN_MORPHIK_TOKEN", "secret")
        assert validate_tools(tmpl) == []

    def test_send_email_requires_all_smtp_vars(self, monkeypatch):
        # smtp_enabled needs host AND username AND password AND from_address.
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="T",
            description="T",
            actions=["reply"],
            tools=TemplateTools(required=["send_email"]),
        )
        for v in (
            "KAI_SMTP_TOOL_HOST",
            "KAI_SMTP_TOOL_USERNAME",
            "KAI_SMTP_TOOL_PASSWORD",
            "KAI_SMTP_TOOL_FROM_ADDRESS",
        ):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("KAI_SMTP_TOOL_HOST", "smtp.example.com")  # host only
        assert validate_tools(tmpl)  # still missing — needs all four
        monkeypatch.setenv("KAI_SMTP_TOOL_USERNAME", "u")
        monkeypatch.setenv("KAI_SMTP_TOOL_PASSWORD", "p")
        monkeypatch.setenv("KAI_SMTP_TOOL_FROM_ADDRESS", "a@b.c")
        assert validate_tools(tmpl) == []


class TestActionVocabularyDrift:
    """Guard against the resolver's per-transport action sets drifting from the
    bot modules' canonical definitions. If a bot adds/renames an action without
    updating resolver.py, validate_actions() would wrongly reject valid
    templates — these tests make that drift fail loudly instead.
    """

    def test_waha_actions_match_bot_definition(self):
        assert set(_FULL_ACTION_NAMES) == _WAHA_VALID_ACTIONS

    def test_email_actions_match_bot_definition(self):
        email_literal_args = set(get_args(EmailAction.model_fields["action"].annotation))
        assert email_literal_args == _EMAIL_VALID_ACTIONS
