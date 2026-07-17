import pytest

from kai.templates import TemplateRegistry
from kai.templates.resolver import validate_actions

ALL_KNOWN_TOOL_NAMES = frozenset(
    [
        "web_search",
        "get_webpage_content",
        "get_current_datetime",
        "get_weather",
        "calculate",
        "schedule_task",
        "list_tasks",
        "cancel_task",
        "record_note",
        "get_conversation_messages",
        "brain_query",
        "sql_query",
        "describe_tables",
        "send_email",
        "calcom",
        "get_available_slots",
        "schedule_event",
        "get_whatsapp_history",
        "escalate",
        "blacklist_contact",
    ]
)


@pytest.mark.parametrize(
    "tmpl",
    TemplateRegistry.bundled().list(),
    ids=lambda t: f"{t.transport}/{t.name}",
)
class TestTemplateToolDeclarations:
    def test_required_tools_exist(self, tmpl):
        for tool in tmpl.tools.required:
            assert tool in ALL_KNOWN_TOOL_NAMES, (
                f"{tmpl.transport}/{tmpl.name}: required tool {tool!r} not "
                f"found in known tool registry"
            )

    def test_optional_tools_exist(self, tmpl):
        for tool in tmpl.tools.optional:
            assert tool in ALL_KNOWN_TOOL_NAMES, (
                f"{tmpl.transport}/{tmpl.name}: optional tool {tool!r} not "
                f"found in known tool registry"
            )

    def test_actions_are_valid(self, tmpl):
        # Transport-aware: an email template declaring `send_voice_note` must
        # fail here, so we reuse the resolver's validator rather than a
        # transport-blind allow-list.
        errors = validate_actions(tmpl)
        assert errors == [], f"{tmpl.transport}/{tmpl.name}: invalid actions: {errors}"
