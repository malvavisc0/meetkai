# kAI

> A hackable Python framework for building and running small LLM agents over anything that emits a webhook.

kAI gives agents a runtime, memory, tools, scheduling, and a simple plugin shape.
Bring any transport that can deliver events over HTTP: WhatsApp, email,
Telegram, a Docker alert stream, or an internal system webhook.

Think of each agent as an employee: it receives an input, decides the best
action for it (reply, stay silent, escalate the recipient, schedule a
follow-up), and executes that decision itself — there is no separate "send"
tool the model calls. An operator (the person running the agent) can supervise
and steer it through the cockpit's console, connect it to a knowledge base
(the Brain), and switch it on or off, but the moment-to-moment decisions are
the agent's.

## What kAI Builds

- **Support agents** that answer WhatsApp or email questions with shared memory
  and a shared knowledge base.
- **DevOps agents** that receive alerts, inspect systems, and run approved
  actions.
- **Process agents** that watch a queue, triage incoming work, and report
  status.

Same runtime, different bot plugins.

Onboarding an agent mirrors hiring an employee:

1. **Choose a role** — support, inbox, or triage: the work you want covered.
2. **Train its Brain** — add documents, a website, notes, and the rules that
   should guide company-specific replies.
3. **Connect work** — add the channels where work arrives and the tools the
   agent can use.
4. **Supervise** — review what it handles, refine its setup, and stop it
   whenever needed.

## What's Included

- **Bot runtime** with OpenAI-compatible LLM access, per-conversation history,
  runtime goals, and scheduled tasks.
- **Plugin contract** through `BaseBot`, so each bot only defines its
  transport, prompt, action vocabulary, settings, and tools.
- **Structured decisions, not free text.** Every bot turn ends in a typed
  `ActionResult` (e.g. `reply`, `silent`, `console`) the model must choose
  from — the bot dispatches it, it is never inferred from prose.
- **Cockpit** for operators to manage connections, deployments, agent settings,
  chat with a running agent (the operator console), and the Brain.
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
database), create deployments, adjust agent settings, chat with deployed
agents through the operator console, and add knowledge to the Brain.

## Operating A Running Bot

Every bot that opts in exposes a `/tell` HTTP route (wired by the framework,
handled by the bot's `handle_operator`). This is the one channel an operator
uses to supervise a running agent:

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

- **WhatsApp support agent**: answer questions in chats using the Brain and
  conversation history (this is `waha`).
- **Docker watchdog**: receive container alerts, inspect state, and run
  approved remediation commands.
- **Email triage agent**: route inbound mail into a webhook, draft replies, and
  keep thread-specific history (this is `email`).

## Development

```bash
uv run pytest            # runs across all available CPU cores via pytest-xdist
uv run pytest -n 0       # disable parallelism (single process)
uv run pytest -n 4       # pin worker count
uv run ruff format .
uv run ruff check --fix .
uv run ruff check .
uv lock --upgrade && uv sync
```

## Running Locally (Dev Stack)

The repo ships a single `docker-compose.yml` (base, what Coolify deploys to
production) plus a `docker-compose.override.yml` (dev-only, auto-loaded by
`docker compose`). Local dev runs the full stack — redis, waha, morphik,
morphik-postgres, crawl4ai, mailpit — and builds the cockpit image from the
working tree instead of pulling it.

Prerequisites:
- Docker + Docker Compose v2
- A `.env` file at the repo root with all variables referenced by the
  compose files (copy from `.env.example` and fill in secrets)

Start the dev stack:

```bash
docker compose up -d --build
```

`docker compose` automatically merges `docker-compose.yml` +
`docker-compose.override.yml`, so dev gets:
- the cockpit built locally (`build: .`, image `meetkai-dev-cockpit`)
- mailpit (SMTP catch + web UI on host port 8025)
- a separate `meetkai-dev` compose project (isolated volumes)

Reach the cockpit at `http://localhost:8080`, mailpit UI at
`http://localhost:8025`, and morphik at `http://localhost:8000` (internal only
unless you publish it).

Notes:
- The morphik config lives inline in `docker-compose.yml` under
  `configs.morphik_config` (mounted at `/app/morphik.toml`); edit it there,
  not in a separate file.
- Production (Coolify) deploys with `docker compose -f docker-compose.yml`,
  which ignores the override: no mailpit, cockpit pulls the published image
  and exposes `8080` (no published host port; Coolify's Caddy proxy routes
  the public domain to it).

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
