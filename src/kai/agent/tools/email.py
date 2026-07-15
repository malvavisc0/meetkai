"""send_email — send an email as the operator's configured SMTP account.

Bot-agnostic: ``cli/bot.py:_start()`` reads ``SmtpSettings`` from env vars
(``KAI_SMTP_TOOL_*``), and when ``smtp_enabled`` is true, calls
:func:`register_email_tool`, which registers the tool on the agent AND
injects a workflow-guidance block into the system prompt.

The ``from`` address is closed over from the deployment's env — the LLM
cannot override it (spoofing guard). The password lives only in the closure
and never appears in any tool argument, result, or log record.

The workflow instruction composes — appended alongside any other workflow
blocks (the waha bot's web-search, the Brain's, the SQL tool's) rather
than replacing them (``agent/core.py:set_tool_workflow``).
"""

from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from llama_index.core.tools import FunctionTool
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_NO_REPLY = re.compile(r"^(no[-_]?reply|donotreply)@", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Single source of truth for the fallback sender identity — every
# BotConfig.display_name field default, each bot's own _load_config
# fallback, and the cockpit settings form's fallback all derive from this
# one constant instead of repeating the literal (see bots/email/setup.py,
# bots/waha/setup.py, bots/base.py::BaseBot.display_name(), and
# cockpit/routes/deployments/settings.py).
DEFAULT_DISPLAY_NAME = "Knowledgeable AI"


class SmtpSettings(BaseSettings):
    """SMTP tool settings — read from KAI_SMTP_TOOL_* env vars.

    Distinct from ``cockpit/mailer.py``'s ``KAI_SMTP_*`` (the cockpit's own
    login-link relay). One is the kai install's mail relay; the other is the
    operator's sending account for the agent tool.
    """

    model_config = SettingsConfigDict(env_prefix="KAI_SMTP_TOOL_", env_file=".env", extra="ignore")

    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    from_address: str = ""
    use_tls: bool = True
    instruction: str = ""

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.host and self.username and self.password and self.from_address)


def get_smtp_settings() -> SmtpSettings:
    return SmtpSettings()


class _EmailToolAgent(Protocol):
    def register_tool(self, tool: FunctionTool) -> None: ...

    def set_tool_workflow(self, workflow: str | None) -> None: ...


def _valid_recipient(addr: str) -> bool:
    """Reject obviously invalid / self-defeating destinations.

    Not an allowlist — arbitrary recipients are allowed; only ``no-reply@``
    (and variants) and bad syntax are rejected.
    """
    a = (addr or "").strip()
    if not a or not _EMAIL_RE.match(a):
        return False
    if _NO_REPLY.match(a):
        return False
    return True


def format_from_header(display_name: str, address: str) -> str:
    """Render an RFC 2822 ``From`` header value for an outbound email.

    Shared by ``make_send_email_tool`` (the agent tool) and the email bot's
    ``_send_reply`` — one implementation so the two call sites can't drift
    the way the hardcoded "Knowledgeable AI" literal already had.
    """
    return formataddr((display_name, address))


def build_email_workflow_instruction(instruction: str) -> str:
    """Render the operator's email usage rules into a workflow prompt block.

    Empty instruction = a minimal default that just tells the agent the tool
    exists and the from address is fixed. Non-empty = the operator's
    free-text rules, one trigger per line, appended as guidance.
    """
    base = (
        "You have a tool called `send_email` for sending emails from the "
        "operator's configured SMTP account. The from address is fixed to "
        "the operator's account; you cannot set it. You set to, subject, "
        "and body."
    )
    triggers = [ln.strip() for ln in instruction.splitlines() if ln.strip()]
    if not triggers:
        return base
    body = "\n".join(f"- {ln}" for ln in triggers)
    return f"{base}\nUse it when:\n{body}"


def send_via_smtp(
    msg: EmailMessage,
    *,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    use_tls: bool = True,
    timeout: int = 30,
) -> None:
    """Send a pre-built ``EmailMessage`` via SMTP.

    Shared by ``make_send_email_tool`` (the agent tool) and the email bot's
    ``_send_reply`` — one implementation of the connect/starttls/auth/send
    sequence so STARTTLS-availability checks and ordering can't drift.

    Raises on any failure; callers decide how to surface the error.
    """
    with smtplib.SMTP(host, int(port), timeout=timeout) as server:
        server.ehlo()
        if use_tls:
            if not server.has_extn("starttls"):
                raise RuntimeError("SMTP server does not support STARTTLS")
            server.starttls()
            server.ehlo()
        if username and password:
            server.login(username, password)
        server.send_message(msg)


def make_send_email_tool(
    host: str,
    port: int,
    username: str,
    password: str,
    from_address: str,
    *,
    use_tls: bool = True,
    display_name: str = DEFAULT_DISPLAY_NAME,
) -> FunctionTool:
    """Build the ``send_email`` tool bound to the operator's SMTP config.

    The ``from_address``, ``host``, ``port``, ``username``, and ``password``
    are closed over — the LLM cannot override any of them. The tool's
    signature is ``send_email(to, subject, body)`` with no ``from``.
    ``display_name`` is the calling bot's own configured identity (its
    ``BotConfig.display_name``), not the shared SMTP credential's.
    """

    def send_email(to: str, subject: str, body: str) -> str:
        """Send an email from the operator's configured account.

        The from address is fixed to the operator's account; you cannot set
        it. Use for sending the result of a task or a notification the user
        asked for.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.
        """
        if not _valid_recipient(to):
            return f"Error: invalid recipient address: {to!r}"
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = format_from_header(display_name, from_address)
            msg["To"] = to
            msg.set_content(body)
            send_via_smtp(
                msg,
                host=host,
                port=port,
                username=username,
                password=password,
                use_tls=use_tls,
            )
            return "sent"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result
            logger.exception("send_email failed")
            return f"Error: send failed ({exc})"

    return FunctionTool.from_defaults(
        fn=send_email,
        name="send_email",
        description=(
            "Send an email from the operator's configured SMTP account. "
            "The from address is fixed; you set to, subject, and body."
        ),
    )


def register_email_tool(
    agent: _EmailToolAgent,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    from_address: str,
    use_tls: bool = True,
    instruction: str = "",
    display_name: str = DEFAULT_DISPLAY_NAME,
) -> None:
    """Register send_email on agent and inject the workflow prompt.

    Mirrors ``register_brain_tool`` / ``register_sql_tool``: registers the
    tool and appends a workflow instruction block to the system prompt.
    No persistent resource to return (smtplib connections are short-lived).
    """
    tool = make_send_email_tool(
        host,
        int(port),
        username,
        password,
        from_address,
        use_tls=use_tls,
        display_name=display_name,
    )
    agent.register_tool(tool)
    agent.set_tool_workflow(build_email_workflow_instruction(instruction))
