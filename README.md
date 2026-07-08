# KAI

> A hackable Python framework for building and running small LLM bots over anything that emits a webhook.

KAI gives bots a runtime, memory, tools, scheduling, and a simple plugin shape.
Bring any transport that can deliver events over HTTP: WhatsApp, email,
Telegram, a Docker alert stream, or an internal system webhook.

The bundled `waha` bot runs on WhatsApp through WAHA. It is a complete example,
not the limit of what KAI is for.

## What KAI Builds

- **Support bots** that answer WhatsApp or email questions with shared memory.
- **DevOps bots** that receive alerts, inspect systems, and run approved actions.
- **Process bots** that watch a queue, triage incoming work, and report status.

Same runtime, different bot plugins.

## What's Included

- **Bot runtime** with OpenAI-compatible LLM access, per-conversation history,
  runtime goals, and scheduled tasks.
- **Plugin contract** through `BaseBot`, so each bot only defines its transport,
  prompt, settings, and tools.
- **Cockpit** for Operators to manage connections, deployments, bot settings,
  chat, and the Brain.
- **Brain** for knowledge ingestion and bot memory from documents, notes, and
  websites.
- **Default tools** for web search, webpage fetch, weather, time, calculator,
  and hardware info.
- **Per-bot isolation** for config, history, and scheduled tasks.

## Current Bot

[`waha`](src/kai/bots/waha/README.md) is a WhatsApp bot via
[WAHA](https://github.com/devlikeapro/waha). It can reply in chats and groups,
handle mentions, voice notes, images, and video, and participate proactively.

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

KAI reads `.env` from the working directory. Core settings use the `KAI_`
prefix. Bot-specific settings use their own prefixes, such as `KAI_WAHA_`.

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

Operators use the cockpit to connect services, create deployments, adjust bot
settings, chat with deployed bots, and add knowledge to the Brain.

## Write A Bot

A bot is a package under `src/kai/bots/<name>/` that exposes a `Bot` class
subclassing `BaseBot`. Each bot owns its config and prompt.

```python
from kai.agent.core import KaiAgent
from kai.bots.base import BaseBot
from kai.config.prompts import load_system_prompt
from kai.config.settings import Settings


class Bot(BaseBot):
    name = "mybot"

    def configure(self, agent: KaiAgent, settings: Settings) -> None:
        super().configure(agent, settings)
        prompt = load_system_prompt(
            str(self.bot_dir / "prompt.md"),
            variables={"language": "English"},
        )
        agent.set_system_prompt(prompt)
        # agent.register_tool(...)  # add bot-specific capabilities

    async def run(self) -> None:
        # Start the transport and route inbound events to:
        # agent.chat() for replies, or agent.observe() for memory-only context.
        ...
```

`configure()` loads the prompt, registers tools, and prepares the bot. `run()`
starts the transport and passes incoming events into the agent.

## Bot Ideas

- **WhatsApp support bot**: answer questions in chats using the Brain and
  conversation history.
- **Docker watchdog**: receive container alerts, inspect state, and run approved
  remediation commands.
- **Email triage bot**: route inbound mail into a webhook, draft replies, and
  keep thread-specific history.

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
│   ├── bots/           # Bot plugins
│   ├── brain/          # Brain client and ingestion helpers
│   ├── cli/            # Command-line interface
│   ├── cockpit/        # Operator web cockpit
│   ├── config/         # Settings and prompts
│   ├── logging/
│   ├── runs.py
│   └── vendors/
└── tests/
```
