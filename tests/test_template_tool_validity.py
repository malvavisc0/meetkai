import pytest

from kai.templates import TemplateRegistry
from kai.templates.resolver import KNOWN_TOOL_NAMES, validate_actions


@pytest.mark.parametrize(
    "tmpl",
    TemplateRegistry.bundled().list(),
    ids=lambda t: f"{t.transport}/{t.name}",
)
class TestTemplateToolDeclarations:
    def test_required_tools_exist(self, tmpl):
        for tool in tmpl.tools.required:
            assert tool in KNOWN_TOOL_NAMES, (
                f"{tmpl.transport}/{tmpl.name}: required tool {tool!r} not "
                f"found in known tool registry"
            )

    def test_optional_tools_exist(self, tmpl):
        for tool in tmpl.tools.optional:
            assert tool in KNOWN_TOOL_NAMES, (
                f"{tmpl.transport}/{tmpl.name}: optional tool {tool!r} not "
                f"found in known tool registry"
            )

    def test_bot_tools_exist(self, tmpl):
        for tool in tmpl.tools.bot_tools:
            assert tool in KNOWN_TOOL_NAMES, (
                f"{tmpl.transport}/{tmpl.name}: bot_tools entry {tool!r} not "
                f"found in known tool registry"
            )

    def test_actions_are_valid(self, tmpl):
        # Transport-aware: an email template declaring `send_voice_note` must
        # fail here, so we reuse the resolver's validator rather than a
        # transport-blind allow-list.
        errors = validate_actions(tmpl)
        assert errors == [], f"{tmpl.transport}/{tmpl.name}: invalid actions: {errors}"
