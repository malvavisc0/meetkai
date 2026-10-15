# Python Developer AI Agent

## Communication
You are talking to seniors. Be terse — no preamble, no recap, no
hand-holding, no "here's what I did" summaries. State results, not
intentions. Skip explanations unless explicitly asked. Never narrate
tool use. Answer the question; move on.

## Project
kai — a hackable Python framework for building and running small LLM
agents over anything that emits a webhook: email, Telegram, a Docker
alert stream, or an internal system webhook. Bots are plugin packages
(`BaseBot` subclasses under `src/kai/bots/`) that share one runtime —
LLM agent, memory, tools, scheduler — and one webhook contract. A typer
CLI (`kai`) runs the operator cockpit (FastAPI + server-rendered Jinja
UI, `src/kai/cockpit/`) and per-bot webhook servers, with
HMAC-authenticated webhooks and pydantic-settings config under the
`KAI_` prefix.

## Stack
- python 3.13+, uv, pyproject.toml (hatchling backend, src layout)
- typer CLI (`kai` entrypoint, `src/kai/cli/`), FastAPI webhook servers
  (`src/kai/bots/webhook.py`, uvicorn), cockpit operator UI
  (`src/kai/cockpit/`, Jinja + static CSS)
- Pydantic models and pydantic-settings for config
  (`src/kai/config/settings.py`, `KAI_` prefix, real env vars win;
  bot settings under their own `KAI_BOT_`/bot-specific prefixes)
- HMAC webhook verification (`src/kai/bots/webhook.py`) — always
  enforced; key per bot in its settings (`hmac_key`, sha512 default)
- SQLAlchemy + alembic for the cockpit database
- stdlib `logging` via `src/kai/logging` (console + file; uvicorn/httpx
  silenced to WARNING)
- Type checker: **pyright** (`[tool.pyright]` in pyproject.toml)
- Lint/format: **ruff** (line-length 100)
- Tests: **pytest** + pytest-asyncio (`asyncio_mode = "auto"`),
  **pytest-xdist** (`-n auto`)

## Commands
```bash
uv sync                                # install deps
uv run kai --help                      # CLI surface
uv run kai cockpit serve               # run the cockpit
uv run kai start <bot> […]             # run a bot
uv run ruff check --fix .              # lint
uv run ruff format .                   # format
uv run pyright                         # type check
uv run pytest                          # test suite (-n auto, xdist)
uvx radon cc src -s | grep -E '-\s(C|D|E|F)\s'   # complexity gate (must be empty)
uvx vulture src --min-confidence 60    # dead code
```
Run `ruff check`, `ruff format --check`, `pyright`, and the relevant
tests after every change.

## Complexity budget
`radon cc` (cyclomatic complexity) must report **no block ranked C or
worse** — every function/method stays at rank A or B (complexity ≤ 10).
This applies to the whole codebase, not just new code: **a pre-existing
C/D/E/F block is still a defect and must always be fixed when
encountered** — never worked around, never left "for later". When a
change pushes a block to C+ (or you touch a file containing one), split
it into smaller functions (early returns, extract helpers, dispatch
tables) instead of adding `if` branches, and fix any pre-existing C+
blocks in the same pass. Verify with:
```bash
uvx radon cc src -s | grep -E '-\s(C|D|E|F)\s'   # must be empty
```

## Dead code
Rules say "no dead code"; vulture enforces it (`uvx vulture src
--min-confidence 60`). A hit is a *suspect*: check for a registration
site before deleting — typer commands, FastAPI routes, and pydantic
members are framework-dispatched and legitimately look unused. Real
dead code (never-read fields, zero-call-site functions) goes out in the
same pass.

## PII
Never use or commit PII — not in code, tests, docs, plans, fixtures,
examples, or commit messages. PII is any real person's or chat's data:
names, email addresses, phone numbers, group titles, message bodies,
transcripts, trace/session ids tied to a person. Rules:

- Tests, docstrings, and examples use fictional identities only
  (`alice@example.com`-style addresses, invented names and bodies).
  This applies even when the fixture is "just" a name: a real contact's
  address or name in a test is still that person's data.
- Incident write-ups describe the failure; they do not name the people
  or reproduce their messages. Real evidence stays in local, untracked
  artifacts (incident notes, data folders, exports).
- Before committing anything touched by a real conversation, grep the
  diff for names/addresses you saw in it:
  `git diff | rg -i "<name|email|jid>"`. If it came from a chat, it
  does not go in the repo.

## Priority
Clean, simple, maintainable code. Nothing else.

## Commits
One-line messages only. No body, no trailers.

## Rules
- DRY, KISS
- No unnecessary complexity
- No defensive fallbacks / over-engineering
- Explicit > implicit
- Type hints always
- PEP 8 (ruff enforces, line-length 100)
- Meaningful names, small functions
- Fail fast and clearly
- Prefer standard library
- No clever tricks
- Docstrings for public APIs
- No unnecessary inline comments
- Use uv (never pip / system python)
- No mutable module-level globals
- No fake-private `_helpers` — put functions in a module (e.g.
  `bots/webhook.py`) with public names
- No dead code
- No `_`-prefix for methods

## Style
Write the simplest correct solution. Delete anything that isn't needed.

## Conventions
- CLI commands live in `src/kai/cli/` as typer commands in the `kai`
  app; env vars (`KAI_` prefix) override flag defaults.
- Bot plugins live under `src/kai/bots/<name>/` exposing a `Bot`
  subclass of `BaseBot`. Each bot owns its config (pydantic-settings),
  system prompt, and action vocabulary — a typed `ActionResult`
  `Literal` the model must choose from. Actions are dispatch values the
  bot executes, never free text inferred from prose.
- Webhook handlers stay thin: parse → verify (HMAC) → dispatch → log.
  Shared webhook/verification code lives in `src/kai/bots/webhook.py`.
- HMAC verification is mandatory for inbound webhooks — no
  disabled/no-op path. Empty/invalid signature fails closed (401);
  oversized payload is a 413.
- Configuration comes from `.env` via pydantic-settings; real env vars
  take precedence. Add new vars to `.env.example` (and
  `.env.workers.example` when the workers stack needs them).
- The cockpit is a server-rendered Jinja + static CSS UI; keep to the
  primitives in `src/kai/cockpit/static/cockpit.css` and the rules in
  `STYLEGUIDE.md`.
- Brain knowledge (morphik) and per-bot history/tasks persist under the
  configured data folders; never hard-code absolute host paths in code.
