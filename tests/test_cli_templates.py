from pathlib import Path

import yaml
from typer.testing import CliRunner

from kai.cli import app
from kai.cli import templates as cli_templates
from kai.templates import TemplateRegistry

runner = CliRunner()


def _write_template(
    root: Path, transport: str, name: str, data: dict, prompt: str = "PROMPT {{language}}\n"
) -> None:
    d = root / transport / name
    d.mkdir(parents=True)
    (d / "template.yaml").write_text(yaml.dump(data), encoding="utf-8")
    (d / "prompt.md").write_text(prompt, encoding="utf-8")


class TestTemplatesList:
    def test_list_shows_bundled(self):
        result = runner.invoke(app, ["templates", "list"])
        assert result.exit_code == 0
        assert "waha/general" in result.stdout
        assert "email/general" in result.stdout

    def test_list_filter_waha(self):
        result = runner.invoke(app, ["templates", "list", "--transport", "waha"])
        assert result.exit_code == 0
        assert "waha/general" in result.stdout
        assert "email/" not in result.stdout

    def test_list_filter_email(self):
        result = runner.invoke(app, ["templates", "list", "--transport", "email"])
        assert result.exit_code == 0
        assert "email/general" in result.stdout
        assert "waha/" not in result.stdout

    def test_list_empty_transport(self):
        result = runner.invoke(app, ["templates", "list", "--transport", "sms"])
        assert result.exit_code == 0
        assert "No templates" in result.stdout


class TestTemplatesShow:
    def test_show_waha_general(self):
        result = runner.invoke(app, ["templates", "show", "waha/general"])
        assert result.exit_code == 0
        assert "Kai" in result.stdout
        assert "actions" in result.stdout

    def test_show_email_general(self):
        result = runner.invoke(app, ["templates", "show", "email/general"])
        assert result.exit_code == 0
        assert "Kai" in result.stdout

    def test_show_missing_exits_nonzero(self):
        result = runner.invoke(app, ["templates", "show", "waha/nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.stdout.lower()

    def test_show_bad_format_exits_nonzero(self):
        result = runner.invoke(app, ["templates", "show", "no-slash"])
        assert result.exit_code != 0

    def test_show_unknown_transport_exits_nonzero(self):
        result = runner.invoke(app, ["templates", "show", "sms/general"])
        assert result.exit_code != 0


class TestTemplatesRender:
    def test_render_waha_general_default_language(self):
        result = runner.invoke(app, ["templates", "render", "waha/general"])
        assert result.exit_code == 0
        # {{language}} substitutes to the default ("English") — a literal
        # "{{language}}" must NOT survive into the rendered output.
        assert "{{language}}" not in result.stdout
        assert "English" in result.stdout

    def test_render_waha_general_language_override(self):
        result = runner.invoke(
            app, ["templates", "render", "waha/general", "--language", "Spanish"]
        )
        assert result.exit_code == 0
        assert "Spanish" in result.stdout
        assert "{{language}}" not in result.stdout

    def test_render_missing_prompt_exits_nonzero(self, tmp_path, monkeypatch):
        # Template exists but has no prompt.md.
        d = tmp_path / "waha" / "noprompt"
        d.mkdir(parents=True)
        (d / "template.yaml").write_text(
            yaml.dump(
                {
                    "name": "noprompt",
                    "transport": "waha",
                    "display_name": "NoPrompt",
                    "description": "T",
                    "actions": ["reply"],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(cli_templates, "_REGISTRY", TemplateRegistry(tmp_path))
        result = runner.invoke(app, ["templates", "render", "waha/noprompt"])
        assert result.exit_code != 0
        assert "prompt" in result.stdout.lower()

    def test_render_includes_escalation_block(self, tmp_path, monkeypatch):
        # Only path that exercises the escalation_rules branch of render() —
        # the bundled general templates have no escalation_rules.
        data = {
            "name": "esc",
            "transport": "waha",
            "display_name": "Esc",
            "description": "T",
            "actions": ["reply"],
            "escalation_rules": [
                {
                    "condition": "Customer asks for a human",
                    "severity": "high",
                    "message": "Customer wants a human agent",
                }
            ],
        }
        _write_template(tmp_path, "waha", "esc", data, prompt="BASE {{language}}\n")
        monkeypatch.setattr(cli_templates, "_REGISTRY", TemplateRegistry(tmp_path))

        result = runner.invoke(app, ["templates", "render", "waha/esc"])
        assert result.exit_code == 0
        assert "ESCALATION RULES" in result.stdout
        assert "Customer asks for a human" in result.stdout
        assert 'severity="high"' in result.stdout
        # Base prompt still rendered + substituted.
        assert "BASE English" in result.stdout

    def test_render_missing_template_exits_nonzero(self):
        result = runner.invoke(app, ["templates", "render", "waha/nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.stdout.lower()
