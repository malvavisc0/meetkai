# email

The `email` bot connects the kAI agent runtime to an inbox through
[Resend](https://resend.com) inbound webhooks and an SMTP reply path. It is
the minimal version of [`waha`](../waha/README.md) — same runtime, same
structured-decision pattern, same operator console — stripped of every
WAHA-specific concern: no groups, no media beyond image attachments, no
voice, no sleep/wake, no organic participation. If you're adding a third bot
type, read this file next to `waha`'s to see what's genuinely
transport-specific versus what `BaseBot`/`KaiAgent` already gives you for
free.

Unlike `waha`, this bot never receives a provider webhook directly. Resend's
signature is verified and the payload normalized by the **cockpit**
(`cockpit/webhooks.py`), which then forwards a plain `NormalizedMessage` to
this bot's own `/ingest` route. The bot's own HTTP server only serves
`/ingest`, `/tell`, `/status`, and `/clear` — all HMAC-verified with a key the
cockpit injects at start time.

## Files

```text
src/kai/bots/email/
├── __init__.py     # Bot class, EmailAction, ingest_event, operator console
├── config.py       # EmailSettings (KAI_BOT_* / KAI_EMAIL_* env vars)
├── setup.py        # BotConfig (language, timezone, blacklist, display_name)
└── prompt.md       # support-bot persona / system prompt
```

The transport client, HMAC webhook server, and inbound-event normalization
are shared with `waha` (`bots/waha/webhook.py`) and the cockpit
(`cockpit/webhooks.py`) rather than duplicated here — this bot has no
`client.py`/`payload.py`/`webhook.py` of its own.

## Resend + SMTP Setup

This bot needs two connections, both created once per operator through the
cockpit (`/connections`), before a deployment of type `email` can start:

1. **Email Inbox (Resend)** — an inbound webhook connection. The cockpit
   shows the exact webhook URL to paste into Resend
   (`/webhook/{your-slug}/resend`) plus the signing secret Resend gives you.
   Resend's webhook carries envelope metadata only (no body/attachments);
   the cockpit fetches the actual body and attachments from Resend's API
   with your API key before forwarding to this bot.
2. **SMTP** — the account this bot sends replies from. Configured with host,
   port, username, password, and TLS, same as any SMTP client.

Both are one-time, per-operator connections — a bot process itself never
touches Resend or reads `.env` for API keys directly; the cockpit injects the
resolved settings as env vars when it starts the bot subprocess.

## Environment

Settings the cockpit injects at start time (you should not normally set
these by hand — they exist so the bot process is self-contained):

```bash
# KAI_BOT_* — control-port + ingest/tell/status HMAC
KAI_BOT_CONTROL_HOST=0.0.0.0
KAI_BOT_CONTROL_PORT=8201          # allocated per-deployment by the cockpit
KAI_BOT_HMAC_KEY=your-secret-key
KAI_BOT_HMAC_ALGORITHM=sha512

# KAI_EMAIL_* — per-deployment feature flags
KAI_EMAIL_VISION=true              # image attachments passed to vision
KAI_EMAIL_MAX_ATTACHMENT_BYTES=10485760

# KAI_SMTP_TOOL_* — the reply path (from the SMTP connection)
KAI_SMTP_TOOL_HOST=smtp.example.com
KAI_SMTP_TOOL_PORT=587
KAI_SMTP_TOOL_USERNAME=bot@example.com
KAI_SMTP_TOOL_PASSWORD=your-password
KAI_SMTP_TOOL_USE_TLS=true
KAI_SMTP_TOOL_FROM_ADDRESS=bot@example.com
```

## Bot Configuration

Config is loaded from `configs/email.json` (relative to the working
directory, configurable via `KAI_CONFIGS_DIR`), same as `waha`. There is no
packaged fallback — if it's missing, the bot runs on `BotConfig()`'s own
field defaults (see `src/kai/bots/email/setup.py`).

`configs/email.json`:

```json
{
    "language": "English",
    "timezone": "Europe/Berlin",
    "temperature": 0.2,
    "blacklist": ["spam@example.com"],
    "display_name": "Kai"
}
```

| Field | Description |
|-------|--------------|
| `language` | Default reply language; overridable per-deployment. |
| `timezone` | IANA timezone the bot tells the model for "current time". |
| `temperature` | LLM sampling temperature. Defaults lower than `waha`'s 0.4 (`0.2`) — a support bot answering from the Brain must ground reliably rather than sound conversational. |
| `blacklist` | Sender addresses to silently ignore, checked fresh from this list on every inbound email — no allowlist concept (unlike `waha`'s whitelist/blacklist pair). |
| `display_name` | Identity shown in the outbound `From` header. |

There is no `whitelist`, `trigger_keyword`, `media`, or `participation`
section — email has no group concept and no "should I speak up" ambient
decision; every inbound email is one-to-one and, by default, answered.

### Prompt

`prompt.md` is the support-bot persona, loaded with `{{language}}`
substituted from config. It is deliberately generic — the bot has **no
built-in product knowledge**; everything it knows comes from the connected
Brain (`brain_query`), never from this prompt or training data. Every turn
returns a structured `EmailAction` (see `__init__.py`), not free text.

## When The Bot Replies

Email is one-to-one correspondence, not an ambient group chat someone can
safely be silent in — the bias is strongly toward `reply`.

**Replies (`action: "reply"`)** to anything with a genuine question, even
short, hard, or ambiguous ones. When the Brain doesn't have the answer, the
bot says so plainly rather than guessing or going silent.

**Stays silent (`action: "silent"`)** only for: content-free connectivity
tests ("ping", "test"), automated/system-generated mail (out-of-office,
bounces, calendar responses, unsubscribe confirmations), pure spam, or
empty/unreadable content.

There is no sleep/wake, no organic participation, and no group-summon logic
— every inbound email either gets a `reply` or is judged not worth one at
all.

## Attachments

Image attachments are downloaded and passed to the model's vision channel
when `KAI_EMAIL_VISION` (the deployment's `image` feature flag) is on;
non-image attachments and images with vision disabled are tagged as
`[attachment: name (content-type)]` text instead of dropped silently.
Downloads are capped by `KAI_EMAIL_MAX_ATTACHMENT_BYTES` (default 10 MiB) —
oversized attachments are skipped with a warning, not retried.

## Operator Console

The bot exposes a `/tell` HTTP route (see the root README's "Operating A
Running Bot"). The agent expresses its decision through the same
`EmailAction` used for inbound turns — `reply`/`console`/`silent` — there is
no separate send tool and no `to` field on the request:

- To answer the operator only ("what's the status of the Brain?"), the agent
  returns `console` with `text` — this goes to the operator's console, never
  to any inbox.
- To send an email, the agent returns `reply` with both `target` (the exact
  address from the instruction) and `text` (the full email body) filled in.
  The address always comes from what the agent read in the operator's own
  words, never from a form field.
- A `reply` with a missing `target` or `text` is reported back as
  `"reply action missing target"` / `"reply action missing text"` rather than
  silently dropped or guessed.

`persist` is accepted for the shared `/tell` contract but has no effect here
— email has no `set_goal` tool wiring (unlike `waha`'s persist-gated
`set_goal`).

## Reliability

- **Blacklist checked fresh on every inbound email** — no persisted block
  history, so unblocking a sender takes effect immediately on the next
  message.
- **HMAC verification:** every route (`/ingest`, `/tell`, `/status`,
  `/clear`) is verified with `KAI_BOT_HMAC_KEY`, same mechanism as `waha`'s
  webhook.
- **SMTP failures are reported, not swallowed:** a failed send on the
  inbound path is logged and the turn is dropped (`ingest_event` catches and
  logs, returning `{"ok": False}`); on an operator turn the failure reason is
  returned in `TellResult.reply` instead.

## CLI

```bash
uv run kai start email
uv run kai status email
```

`start` loads config + prompt, binds the control server, and serves until
interrupted. In practice this bot is started by the cockpit (which allocates
the control port and injects `KAI_BOT_*`/`KAI_EMAIL_*`/`KAI_SMTP_TOOL_*`), not
run standalone the way `waha` can be.
