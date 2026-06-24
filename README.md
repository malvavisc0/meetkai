# kai

> A hackable Python framework for building and running small LLM bots over
> anything that emits a webhook.

Kai gives you an agent runtime and a one-file plugin contract. You bring the
transport — WhatsApp, Telegram, email, a Docker alert feed, anything that
delivers events over HTTP — and Kai wires it to an LLM-backed agent that can
remember conversations, call tools, run on a schedule, and act on the world.

It is not a chat-bot-in-a-box. It is the scaffolding for the bot *you* want:
a WhatsApp group member that chimes in naturally, a Docker watchdog that
catches container alerts and runs remediation commands, an email triage bot
that streamlines a process inbox. Same runtime, three different bots, three
plugins under `src/kai/bots/`.

## Bots

[`waha`](src/kai/bots/waha/README.md) (WhatsApp
via [WAHA](https://github.com/devlikeapro/waha)) — a full group-participant
persona with sleep/wake, organic participation, mentions, and media. It's
here to show what a complete bot looks like, not to define what Kai is for.

## Why kai

Most "LLM bot" projects are a thin wrapper around `chat.completions`: event
in, reply out. Kai is built for the parts that actually matter when a bot
runs unattended against real, noisy inputs.

- **Transport-agnostic core** — OpenAI-compatible LLM access, per-conversation
  history, runtime goals, pluggable tools, and scheduled tasks. The transport
  is just a plugin that turns inbound events into `agent.chat()` /
  `agent.observe()` calls and delivers the reply.
- **A real plugin contract** — `BaseBot` is a class to subclass, not a
  `Protocol` to reimplement. Override a few hooks, don't copy 500 lines.
- **Per-bot isolation** — each bot gets its own history file
  (`data/waha.json`, `data/docker.json`, …) so concurrent bots never clobber
  each other.
- **Runtime goals** — steer a bot live with `--goal`, swapped or cleared
  without a restart. Point the same bot at a different objective on the fly.
- **Scheduled tasks** — the model can schedule one-shot tasks
  (`schedule_task` / `list_tasks` / `cancel_task`) that fire back into the
  originating channel at the right time. Goals are clarity-judged by the LLM
  before they're stored, and a re-entrancy guard stops a fired task from
  spawning another.
- **Per-conversation history** — LRU-bounded, persisted per bot, with a
  configurable cap on messages *and* characters so context never silently
  overflows.
- **Real tools** — DuckDuckGo search, webpage fetch, weather, time,
  calculator, hardware info ship by default; bots register their own (WAHA
  adds `get_chat_history`; a Docker bot would add `docker_run`, etc.).

## What's in the box

- **`kai` CLI** — `start`, `list`, `status`. Clean shutdown on SIGINT, force-quit on the second.
- **`KaiAgent`** — LlamaIndex-backed chat agent with per-conversation history,
  runtime goals, and on-the-fly tool registration.
- **`BaseBot`** — a real plugin contract, not a `Protocol`. Override a few
  hooks, don't copy 500 lines.
- **Tools** — DuckDuckGo web search and webpage fetch ship by default; bots
  add their own.
- **Persona-driven** — each bot's `prompt.md` is its personality, loaded with
  `{{variable}}` substitution. A group-chat friend and a Docker ops bot get
  very different prompts from the same runtime.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- An OpenAI-compatible LLM endpoint
- A transport — anything that can deliver events to a webhook (WAHA, a
  Telegram bot API poller, an email-to-webhook relay, a Docker event stream,
  …). If you can get it into Python, you can write a bot for it.

## Install

```bash
git clone https://github.com/malvavisc0/kai
cd kai
uv sync
```

## Configure

Kai reads `.env` from the working directory. Core settings use the `KAI_`
prefix; transports use their own (e.g. `KAI_WAHA_`).

```bash
# LLM runtime
KAI_LLM_API_BASE=https://api.openai.com/v1
KAI_LLM_API_KEY=sk-your-key-here
KAI_LLM_MODEL=gpt-4o-mini
KAI_LLM_ENABLE_THINKING=false

# Agent behavior
KAI_AGENT_LANGUAGE=English
KAI_AGENT_MAX_HISTORY_MESSAGES=100     # per chat
KAI_AGENT_MAX_HISTORY_CHARS=120000    # per chat
KAI_AGENT_MAX_CONVERSATIONS=256       # total chats (LRU-evicted)
KAI_AGENT_HISTORY_FOLDER=data         # per-bot files written here (data/waha.json)

# Scheduled tasks
KAI_TASKS_ENABLED=true
KAI_TASKS_POLL_INTERVAL_SECONDS=5.0
KAI_TASKS_FOLDER=data                 # <name>.tasks.json per bot

# Logging
KAI_LOG_DIR=/tmp/kai/logs
```

## Usage

```bash
uv run kai list
uv run kai start waha \
  --goal "Be warm, useful, and concise. Only reply when you add value." \
  --language English
uv run kai status waha
```

`start` blocks until SIGINT/SIGTERM — a second signal forces exit. `--language`
overrides the bot's configured language regardless of value. `status` delegates
to the selected bot.

## Write your own bot

A bot is a package under `src/kai/bots/<name>/` exposing a `Bot` class that
subclasses `BaseBot`. Each bot owns its `config.json` and `prompt.md`. The
minimum:

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
        # agent.register_tool(...)  # add transport-specific capabilities

    async def run(self) -> None:
        # Start your transport and route inbound events to
        # agent.chat() (generates a reply) or agent.observe() (context only).
        ...
```

`BaseBot` gives you `resolve_config_path()`, `setup_task_scheduler()`,
`set_task_context()`, and `stop()` / `status()` hooks to override. `configure()`
is where a bot loads its persona, registers tools, and sets up its transport.

**Tool slate.** The agent ships with default chat tools (web search, webpage
fetch, weather, time, calculator, hardware info). A non-chat bot starts clean:

```python
def configure(self, agent, settings):
    super().configure(agent, settings)
    agent.clear_tools()                  # drop web/calculator/weather defaults
    agent.register_tool(docker_inspect)  # only what this bot actually needs
    agent.register_tool(docker_run)
    agent.set_system_prompt(load_system_prompt(...))
    self.setup_task_scheduler(agent, settings)  # re-adds schedule_task etc.
```

`agent.set_tool_workflow(...)` optionally appends tool-usage guidance to the
system prompt. The bundled `WEB_WORKFLOW_INSTRUCTIONS` (fact-checking via
search) is opted into by the `waha` chat bot; a Docker/email bot leaves it off
for a clean prompt.

A few things this same shape can build:

- **A WhatsApp group member** — the bundled `waha` bot: sleep/wake, organic
  participation, mentions, media, voice transcription. See
  [`src/kai/bots/waha/`](src/kai/bots/waha/README.md).
- **A Docker watchdog** — ingest container health alerts over a webhook, give
  the agent `docker_run` / `docker_inspect` tools, let it diagnose and
  remediate. Scheduled tasks could poll container state on an interval.
- **An email process bot** — relay inbound mail to a webhook, route each
  thread through the agent with a per-thread goal, send the reply back over
  SMTP. Per-conversation history keeps threads separate.

Same runtime. Three plugins. That's the point.

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
│   ├── cli.py
│   ├── agent/          # KaiAgent, GoalManager, tools, context, scheduler
│   ├── bots/
│   │   ├── base.py     # BaseBot plugin contract
│   │   └── waha/       # example bot: WhatsApp via WAHA (own config, prompt, README)
│   ├── config/         # settings, prompts, message filters
│   ├── logging/
│   └── utils/
└── tests/
```
