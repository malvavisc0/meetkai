"""Tests for the send_email tool + SmtpSettings (Fix 06)."""

from __future__ import annotations

import ast
import inspect
from unittest.mock import MagicMock, patch

import pytest

from kai.agent.tools.email import (
    _valid_recipient,
    build_email_workflow_instruction,
    make_send_email_tool,
    register_email_tool,
)


def _call_tool(tool, **kwargs) -> str:
    """Call a FunctionTool's underlying fn directly (no LLM round-trip)."""
    return tool.fn(**kwargs)


class TestValidRecipient:
    @pytest.mark.parametrize(
        "addr",
        [
            "alice@example.com",
            "bob.user@sub.example.co.uk",
            "user+tag@gmail.com",
        ],
    )
    def test_accepts_valid(self, addr):
        assert _valid_recipient(addr) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "no-reply@example.com",
            "noreply@example.com",
            "DoNotReply@example.com",
            "no_reply@example.com",
            "not-an-email",
            "",
            "   ",
            "@example.com",
            "user@",
            "user@.com",
        ],
    )
    def test_rejects_invalid(self, addr):
        assert _valid_recipient(addr) is False


class TestSendEmailRejects:
    @pytest.fixture
    def email_tool(self):
        return make_send_email_tool("localhost", 1025, "user", "pass", "from@test.com")

    def test_rejects_no_reply(self, email_tool):
        result = _call_tool(email_tool, to="no-reply@example.com", subject="x", body="x")
        assert result.startswith("Error: invalid recipient address:")

    def test_rejects_bad_syntax(self, email_tool):
        result = _call_tool(email_tool, to="not-an-email", subject="x", body="x")
        assert result.startswith("Error: invalid recipient address:")

    def test_rejects_empty(self, email_tool):
        result = _call_tool(email_tool, to="", subject="x", body="x")
        assert result.startswith("Error: invalid recipient address:")


class TestSendEmailConnectionError:
    def test_connection_refused_returns_error_string(self):
        tool = make_send_email_tool("localhost", 1, "", "", "from@test.com", use_tls=False)
        result = _call_tool(tool, to="alice@example.com", subject="test", body="hello")
        assert result.startswith("Error: send failed")


class TestSendEmailSuccess:
    def test_send_called(self):
        tool = make_send_email_tool("smtp.test.com", 587, "user", "pass", "from@test.com")
        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            mock_server.has_extn.return_value = True  # STARTTLS available
            result = _call_tool(tool, to="alice@example.com", subject="Hi", body="Hello")
        assert result == "sent"
        mock_server.starttls.assert_called_once()
        mock_server.send_message.assert_called_once()

    def test_starttls_fail_closed_when_no_starttls(self):
        """When use_tls=True and server doesn't advertise STARTTLS,
        the tool refuses to send (no cleartext password)."""
        tool = make_send_email_tool("smtp.test.com", 587, "user", "pass", "from@test.com")
        with patch("kai.agent.tools.email.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            mock_server.has_extn.return_value = False  # No STARTTLS
            result = _call_tool(tool, to="alice@example.com", subject="Hi", body="Hello")
        assert "does not support STARTTLS" in result
        mock_server.login.assert_not_called()
        mock_server.send_message.assert_not_called()


class TestToolSignatureNoFrom:
    def test_no_from_parameter(self):
        tool = make_send_email_tool("localhost", 1025, "u", "p", "from@test.com")
        import inspect

        sig = inspect.signature(tool.fn)
        param_names = set(sig.parameters.keys())
        assert "to" in param_names
        assert "subject" in param_names
        assert "body" in param_names
        assert "from" not in param_names
        assert "from_address" not in param_names


class TestBuildEmailWorkflowInstruction:
    def test_empty_instruction_has_base_text(self):
        text = build_email_workflow_instruction("")
        assert "send_email" in text
        assert "from address is fixed" in text
        assert "Use it when:" not in text

    def test_non_empty_appends_triggers(self):
        text = build_email_workflow_instruction("reply when asked\nuse professional tone")
        assert "send_email" in text
        assert "Use it when:" in text
        assert "- reply when asked" in text
        assert "- use professional tone" in text


class TestRegisterEmailTool:
    def test_registers_tool_and_workflow(self):
        agent = MagicMock()
        register_email_tool(
            agent,
            host="smtp.test.com",
            port=587,
            username="user",
            password="pass",
            from_address="from@test.com",
            instruction="reply when asked",
        )
        assert agent.register_tool.call_count == 1
        agent.set_tool_workflow.assert_called_once()
        workflow = agent.set_tool_workflow.call_args[0][0]
        assert "send_email" in workflow
        assert "reply when asked" in workflow


class TestNoLoggerInfoInToolFunctions:
    def test_no_logger_info_in_email_module(self):
        from kai.agent.tools import email

        source = inspect.getsource(email)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr == "info"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "logger"
                ):
                    raise AssertionError(
                        f"logger.info call at line {node.lineno} in email.py — "
                        "logging should be in agent/core.py, not duplicated per tool"
                    )
