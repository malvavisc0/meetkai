# kAI

> A hackable Python framework for building and running small LLM bots over anything that emits a webhook.

kAI gives bots a runtime, memory, tools, scheduling, and a simple plugin shape.
Bring any transport that can deliver events over HTTP: WhatsApp, email,
Telegram, a Docker alert stream, or an internal system webhook.

Think of each bot as an employee: it receives an input, decides the best
action for it (reply, stay silent, escalate the recipient, schedule a
follow-up), and executes that decision itself — there is no separate "send"
tool the model calls. An operator (the person running the bot) can supervise
and steer it through the cockpit's console, connect it to a knowledge base
(the Brain), and switch it on or off, but the moment-to-moment decisions are
the agent's.

## What kAI Builds

- **Support bots** that answer WhatsApp or email questions with shared memory
  and a shared knowledge base.
- **DevOps bots** that receive alerts, inspect systems, and run approved
  actions.
- **Process bots** that watch a queue, triage incoming work, and report
  status.

Same runtime, different bot plugins.

## What's Included

- **Bot runtime** with OpenAI-compatible LLM access, per-conversation history,
  runtime goals, and scheduled tasks.
- **Plugin contract** through `BaseBot`, so each bot only defines its
  transport, prompt, action vocabulary, settings, and tools.
- **Structured decisions, not free text.** Every bot turn ends in a typed
  `ActionResult` (e.g. `reply`, `silent`, `console`) the model must choose
  from — the bot dispatches it, it is never inferred from prose.
- **Cockpit** for operators to manage connections, deployments, bot settings,
  chat with a running bot (the operator console), and the Brain.
- **Brain** for knowledge ingestion and per-deployment bot memory from
  documents, notes, and websites.
- **Default tools** for web search, webpage fetch, weather, time, and
  calculator.
- **Per-bot isolation** for config, history, and scheduled tasks.

## Current Bots

- [`waha`](src/kai/bots/waha/README.md) — a WhatsApp employee via
  [WAHA](https://github.com/devlikeapro/waha). Replies in chats and groups,
  handles mentions, voice notes, images, and video, and can participate
  proactively. Supports per-chat sleep/wake.
- [`email`](src/kai/bots/email/README.md) — an email support employee via
  [Resend](https://resend.com) inbound webhooks and an SMTP reply path.
  Answers grounded in the Brain, one address at a time, with no group/media/
  participation concerns.

Both bots share the same runtime, the same operator console pattern (steer
via `/tell`, the agent picks the delivery target through its structured
action, never a form field), and the same Brain integration. `email` is
intentionally the minimal version of `waha` — read its README alongside
`waha`'s to see what a new bot has to add versus what it gets for free from
`BaseBot`/`KaiAgent`.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- An OpenAI-compatible LLM endpoint
- A transport that can deliver events to a webhook

## Install

```bash
git clone https://github.com/malvavisc0/kai
cd kai
uv sync
```

## Configure

kAI reads `.env` from the working directory. Core settings use the `KAI_`
prefix. Bot-specific settings use their own prefixes, such as `KAI_WAHA_` or
`KAI_BOT_`/`KAI_EMAIL_` for the email bot.

```bash
# LLM runtime
KAI_LLM_API_BASE=https://api.openai.com/v1
KAI_LLM_API_KEY=sk-your-key-here
KAI_LLM_MODEL=gpt-4o-mini
KAI_LLM_ENABLE_THINKING=false

# Agent behavior
KAI_AGENT_LANGUAGE=English
KAI_AGENT_MAX_HISTORY_MESSAGES=100
KAI_AGENT_MAX_HISTORY_CHARS=120000
KAI_AGENT_MAX_CONVERSATIONS=256
KAI_AGENT_HISTORY_FOLDER=data

# Scheduled tasks
KAI_TASKS_ENABLED=true
KAI_TASKS_POLL_INTERVAL_SECONDS=5.0
KAI_TASKS_FOLDER=data

# Logging
KAI_LOG_DIR=/tmp/kai/logs
```

## Run A Bot

```bash
uv run kai list
uv run kai start waha \
  --goal "Be warm, useful, and concise. Only reply when you add value." \
  --language English
uv run kai status waha
```

`start` runs until SIGINT or SIGTERM. A second signal forces exit. `status`
delegates to the selected bot.

## Run The Cockpit

```bash
uv run kai cockpit serve
```

Operators use the cockpit to connect services (WhatsApp, SMTP, Resend,
database), create deployments, adjust bot settings, chat with deployed bots
through the operator console, and add knowledge to the Brain.

## Operating A Running Bot

Every bot that opts in exposes a `/tell` HTTP route (wired by the framework,
handled by the bot's `handle_operator`). This is the one channel an operator
uses to supervise a running "employee":

- Send it a plain instruction ("what's the status of the last email from
  alice@example.com?", "send an email to bob@example.com with the shipping
  update"). The agent decides what to do through its own structured action —
  there is no `to`/target form field the operator fills in; the address
  always comes from what the agent read in the instruction.
- Pass `persist=true` to have the agent turn a steering instruction into a
  standing goal (`set_goal`), when the bot type supports it.
- The response is a structured `TellResult` (`ok`, `actions`, `reply`) — the
  cockpit's chat console renders `reply`, and shows a delivery confirmation
  only when the bot's own response confirms it actually sent something.

## Write A Bot

A bot is a package under `src/kai/bots/<name>/` that exposes a `Bot` class
subclassing `BaseBot`. Each bot owns its config, prompt, and action
vocabulary — the runtime (history, tool loop, scheduler, `/tell` HTTP
contract) is shared.

```python
from typing import Literal

from pydantic import Field

from kai.agent.core import ActionResult, KaiAgent
from kai.bots.base import BaseBot
from kai.config.prompts import load_system_prompt
from kai.config.settings import Settings


class MyBotAction(ActionResult):
    """The only vocabulary the model may choose from for this bot."""

    action: Literal["reply", "silent"] = Field(  # type: ignore[assignment]
        description="'reply' to answer, 'silent' to say nothing."
    )
    text: str | None = None


class Bot(BaseBot):
    name = "mybot"

    def configure(self, agent: KaiAgent, settings: Settings, *, voice: str | None = None) -> None:
        super().configure(agent, settings)
        prompt = load_system_prompt(
            str(self.bot_dir / "prompt.md"),
            variables={"language": "English"},
        )
        agent.set_system_prompt(prompt)
        # agent.register_tool(...)  # add bot-specific capabilities

    async def run(self) -> None:
        # Start the transport and route inbound events into:
        #   agent.chat(text, output_cls=MyBotAction, ...) for a decision, or
        #   agent.observe(text, ...) for memory-only context (no decision).
        # Then dispatch result.action.action yourself — actions are never
        # tools the model calls, they are the value it returns.
        ...
```

`configure()` loads the prompt, registers tools, and prepares the bot. `run()`
starts the transport and passes incoming events into the agent. The bot
itself owns turning the returned `ActionResult` into a side effect (send an
email, post a WhatsApp message, do nothing) — the framework only guarantees
the model answered with one of the values your `Literal` allows.

## Bot Ideas

- **WhatsApp support bot**: answer questions in chats using the Brain and
  conversation history (this is `waha`).
- **Docker watchdog**: receive container alerts, inspect state, and run
  approved remediation commands.
- **Email triage bot**: route inbound mail into a webhook, draft replies, and
  keep thread-specific history (this is `email`).

## Development

```bash
uv run pytest
uv run ruff format .
uv run ruff check --fix .
uv run ruff check .
uv lock --upgrade && uv sync
```

## Layout

```text
kai/
├── pyproject.toml
├── README.md
├── src/kai/
│   ├── agent/          # LLM runtime, tools, goals, context, scheduling
│   ├── bots/           # Bot plugins (waha, email)
│   ├── brain/          # Brain client and ingestion helpers
│   ├── cli/            # Command-line interface
│   ├── cockpit/        # Operator web cockpit
│   ├── config/         # Settings and prompts
│   ├── logging/
│   ├── runs.py
│   └── vendors/
└── tests/
```
