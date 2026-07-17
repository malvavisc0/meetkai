from pathlib import Path

import pytest
import yaml

from kai.templates import TemplateRegistry
from kai.templates.schema import PostProcessingConfig


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


class TestTemplateRegistry:
    def test_list_all(self):
        reg = TemplateRegistry.bundled()
        templates = reg.list()
        names = [f"{t.transport}/{t.name}" for t in templates]
        assert "waha/general" in names
        assert "email/general" in names

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
    def test_tool_configured_map_for_general(self):
        from kai.templates.resolver import tool_configured_map

        reg = TemplateRegistry.bundled()
        tmpl = reg.get("waha", "general")
        result = tool_configured_map(tmpl)
        assert "brain_query" in result


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
