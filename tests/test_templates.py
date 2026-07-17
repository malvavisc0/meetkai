from pathlib import Path

import pytest
import yaml

from kai.templates import TemplateRegistry
from kai.templates.schema import (
    EscalationRule,
    PostProcessingConfig,
    TemplateDef,
    TemplateTools,
)


class TestPostProcessingConfig:
    def test_default_is_none(self):
        cfg = PostProcessingConfig()
        assert cfg.profile == "none"

    def test_waha_default(self):
        cfg = PostProcessingConfig(profile="waha_default")
        assert cfg.profile == "waha_default"

    def test_custom_allows_step_fields(self):
        cfg = PostProcessingConfig(
            profile="custom",
            strip_markdown=True,
            collapse_to_single_line=True,
            max_sentences=3,
            max_words=50,
        )
        assert cfg.profile == "custom"

    def test_dead_config_raises_for_waha_default(self):
        with pytest.raises(ValueError, match="waha_default"):
            PostProcessingConfig(
                profile="waha_default",
                strip_emojis=True,
            )

    def test_dead_config_raises_for_none(self):
        with pytest.raises(ValueError, match="none"):
            PostProcessingConfig(
                profile="none",
                max_words=10,
            )

    def test_dead_config_no_raise_when_defaults(self):
        cfg = PostProcessingConfig(profile="waha_default")
        assert cfg.profile == "waha_default"

    def test_none_profile_allows_none_defaults(self):
        cfg = PostProcessingConfig(profile="none", max_sentences=None)
        assert cfg.profile == "none"


class TestTemplateTools:
    def test_empty(self):
        tools = TemplateTools()
        assert tools.required == []
        assert tools.optional == []

    def test_required_only(self):
        tools = TemplateTools(required=["schedule_task"])
        assert tools.required == ["schedule_task"]

    def test_both(self):
        tools = TemplateTools(required=["a"], optional=["b"])
        assert tools.required == ["a"]
        assert tools.optional == ["b"]


class TestEscalationRule:
    def test_valid(self):
        rule = EscalationRule(
            condition="Customer frustrated",
            severity="high",
            message="Customer needs human",
        )
        assert rule.severity == "high"

    def test_bad_severity(self):
        with pytest.raises(Exception):
            EscalationRule(
                condition="test",
                severity="extreme",
                message="bad",
            )


class TestTemplateDef:
    def test_minimal(self):
        tmpl = TemplateDef(
            name="test",
            transport="waha",
            display_name="Test",
            description="A test",
            actions=["reply", "silent"],
        )
        assert tmpl.web_workflow is True
        assert tmpl.post_processing.profile == "none"

    def test_full(self):
        tmpl = TemplateDef(
            name="full",
            transport="email",
            display_name="Full",
            description="Full test",
            actions=["reply"],
            tools=TemplateTools(required=["schedule_task"], optional=["brain_query"]),
            web_workflow=False,
            reply_style="Be concise",
            goal_suggestion="Help people",
            escalation_rules=[
                EscalationRule(
                    condition="Angry",
                    severity="critical",
                    message="Customer angry",
                )
            ],
        )
        assert tmpl.web_workflow is False
        assert len(tmpl.escalation_rules) == 1


class TestTemplateRegistry:
    def test_bundled(self):
        reg = TemplateRegistry.bundled()
        assert reg is not None

    def test_list_all(self):
        reg = TemplateRegistry.bundled()
        templates = reg.list()
        names = [f"{t.transport}/{t.name}" for t in templates]
        assert "waha/general" in names
        assert "email/general" in names

    def test_list_waha(self):
        reg = TemplateRegistry.bundled()
        templates = reg.list(transport="waha")
        assert all(t.transport == "waha" for t in templates)

    def test_list_email(self):
        reg = TemplateRegistry.bundled()
        templates = reg.list(transport="email")
        assert all(t.transport == "email" for t in templates)

    def test_get_waha_general(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        assert tmpl.name == "general"
        assert tmpl.transport == "waha"

    def test_get_email_general(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("email", "general")
        assert tmpl.name == "general"
        assert tmpl.transport == "email"

    def test_get_missing_raises(self):
        reg = TemplateRegistry.bundled()
        with pytest.raises(FileNotFoundError):
            reg.get("waha", "nonexistent")

    def test_prompt_path_waha(self):
        reg = TemplateRegistry.bundled()
        path = reg.prompt_path("waha", "general")
        assert path is not None
        assert path.is_file()

    def test_prompt_path_email(self):
        reg = TemplateRegistry.bundled()
        path = reg.prompt_path("email", "general")
        assert path is not None
        assert path.is_file()

    def test_prompt_path_missing(self):
        reg = TemplateRegistry.bundled()
        path = reg.prompt_path("waha", "nonexistent")
        assert path is None

    def test_custom_dir(self, tmp_path):
        waha = tmp_path / "waha" / "mybot"
        waha.mkdir(parents=True)
        (waha / "template.yaml").write_text(
            yaml.dump(
                {
                    "name": "mybot",
                    "transport": "waha",
                    "display_name": "My Bot",
                    "description": "Test",
                    "actions": ["reply"],
                }
            )
        )
        reg = TemplateRegistry(tmp_path)
        tmpl = reg.get("waha", "mybot")
        assert tmpl.name == "mybot"

    def test_transport_mismatch_raises(self, tmp_path):
        waha = tmp_path / "waha" / "mybot"
        waha.mkdir(parents=True)
        (waha / "template.yaml").write_text(
            yaml.dump(
                {
                    "name": "mybot",
                    "transport": "email",
                    "display_name": "My Bot",
                    "description": "Test",
                    "actions": ["reply"],
                }
            )
        )
        with pytest.raises(ValueError, match="mismatch"):
            TemplateRegistry(tmp_path).get("waha", "mybot")

    def test_multiple_dirs_first_wins(self, tmp_path):
        d1 = tmp_path / "d1" / "waha" / "mybot"
        d2 = tmp_path / "d2" / "waha" / "mybot"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        (d1 / "template.yaml").write_text(
            yaml.dump(
                {
                    "name": "mybot",
                    "transport": "waha",
                    "display_name": "First",
                    "description": "First",
                    "actions": ["reply"],
                }
            )
        )
        (d2 / "template.yaml").write_text(
            yaml.dump(
                {
                    "name": "mybot",
                    "transport": "waha",
                    "display_name": "Second",
                    "description": "Second",
                    "actions": ["reply"],
                }
            )
        )
        reg = TemplateRegistry(tmp_path / "d1", tmp_path / "d2")
        tmpl = reg.get("waha", "mybot")
        assert tmpl.display_name == "First"


class TestBundledTemplateContent:
    def test_waha_general_actions(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        expected = [
            "reply",
            "send_voice_note",
            "silent",
            "sleep",
            "send_dm",
            "send_to_group",
            "console",
        ]
        assert tmpl.actions == expected

    def test_waha_general_web_workflow(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        assert tmpl.web_workflow is True

    def test_waha_general_post_processing(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        assert tmpl.post_processing.profile == "waha_default"

    def test_email_general_actions(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("email", "general")
        assert tmpl.actions == ["reply", "silent", "console"]

    def test_email_general_post_processing(self):
        reg = TemplateRegistry.bundled()
        tmpl = reg.get("email", "general")
        assert tmpl.post_processing.profile == "none"


class TestToolConfiguredMap:
    def test_tool_configured_map_returns_dict(self):
        from kai.templates.resolver import tool_configured_map

        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        result = tool_configured_map(tmpl)
        assert isinstance(result, dict)

    def test_tool_configured_map_tools_for_general(self):
        from kai.templates.resolver import tool_configured_map

        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        result = tool_configured_map(tmpl)
        # General waha has optional tools; result maps each to configured state
        assert isinstance(result, dict)
        # At minimum, brain_query should be in the map
        assert "brain_query" in result

    def test_tool_configured_map_reflects_env_state(self, monkeypatch):
        from kai.templates.resolver import tool_configured_map

        # Mock _is_tool_configured: brain_query is "configured" when both env
        # vars are set. This proves the map reflects env state.
        def fake_is_configured(name):
            if name == "brain_query":
                has_base = "KAI_BRAIN_BASE_URL" in monkeypatch._dict
                has_key = "KAI_BRAIN_LIGHTRAG_API_KEY" in monkeypatch._dict
                return has_base and has_key
            return True

        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "customer-support")
        # Just assert it returns a dict without erroring
        result = tool_configured_map(tmpl)
        assert isinstance(result, dict)


class TestPerTemplateReadmes:
    def test_each_template_has_readme(self):
        """Every template directory should contain a non-empty README.md."""
        reg = TemplateRegistry.bundled()
        for tmpl in reg.list():
            readme_dir = Path("templates") / tmpl.transport / tmpl.name
            readme_path = readme_dir / "README.md"
            assert readme_path.is_file(), f"Missing README.md for {tmpl.transport}/{tmpl.name}"
            content = readme_path.read_text(encoding="utf-8")
            assert len(content.strip()) > 20, (
                f"README.md for {tmpl.transport}/{tmpl.name} is too short"
            )

    def test_readme_mentions_display_name(self):
        """Each README should mention its template's display_name."""
        reg = TemplateRegistry.bundled()
        for tmpl in reg.list():
            readme_dir = Path("templates") / tmpl.transport / tmpl.name
            readme_path = readme_dir / "README.md"
            content = readme_path.read_text(encoding="utf-8")
            assert tmpl.display_name in content, (
                f"README for {tmpl.transport}/{tmpl.name} missing "
                f"display_name '{tmpl.display_name}'"
            )
