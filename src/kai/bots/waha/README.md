# waha

The `waha` bot connects the kAI agent runtime to a WhatsApp session through
[WAHA](https://github.com/devlikeapro/waha) webhooks. It receives inbound
messages, routes them to the LLM-backed agent, and sends replies through
WAHA's `/api/sendText`.

It is designed to feel like a natural group participant: it speaks when
addressed, can chime in on its own, stays quiet when sent to sleep, and never
ghosts a direct message.

## Files

```text
src/kai/bots/waha/
├── __init__.py     # Bot class, message handling, sleep/wake, participation
├── actions.py      # WahaAction — the structured decision vocabulary
├── client.py       # WahaClient (httpx) — WAHA HTTP API
├── config.py       # WahaSettings (KAI_WAHA_* env vars)
├── history.py      # get_whatsapp_history tool (reads WAHA, not local history)
├── instagram.py    # Instagram post enrichment
├── jid.py          # WhatsApp ID helpers
├── media.py        # media attachment extraction
├── mentions.py     # @[Name] → WhatsApp mention resolution
├── payload.py      # inbound message parsing
├── processing.py   # reply post-processing, organic participation logic
├── prompt.md       # kAI persona / system prompt
├── seen_store.py   # webhook idempotency (seen message IDs)
├── setup.py        # BotConfig / MediaConfig / ParticipationConfig
├── sleep_store.py  # per-chat sleep state
├── stt.py          # voice note transcription (whisper.cpp)
├── tts.py          # Kokoro voice-reply synthesis
├── video.py        # video compression + audio extraction (ffmpeg)
├── webhook.py      # FastAPI webhook server (HMAC-verified)
├── youtube.py      # YouTube transcript enrichment
└── tests/
```

## WAHA Setup

Start WAHA. For local development:

```bash
docker run -d -p 3000:3000 devlikeapro/waha
```

Create and pair a WAHA session in the WAHA dashboard. The default kAI session
name is `default`. kAI expects the session to already exist and be `WORKING`;
it only registers the session webhook on startup.

Running WAHA via `docker-compose.yml`? Pin a recent release — older wwebjs
builds crashed `GET /api/{session}/chats` with HTTP 500 and dropped inbound
group messages whose sender resolves to an opaque `@lid` (the `_serialized`
id was undefined). Both were fixed upstream in WAHA `2026.7.1`, so the
read-only patch mounts that kAI used to carry are no longer needed.

## Environment

WAHA-specific settings use the `KAI_WAHA_` prefix (loaded from `.env`):

```bash
KAI_WAHA_URL=http://localhost:3000
KAI_WAHA_API_KEY=your-waha-api-key
KAI_WAHA_SESSION=default
KAI_WAHA_WEBHOOK_HOST=0.0.0.0
KAI_WAHA_WEBHOOK_PORT=8000
KAI_WAHA_WEBHOOK_PATH=/webhook/waha
KAI_WAHA_WEBHOOK_PUBLIC_HOST=IP:8000
KAI_WAHA_HMAC_KEY=your-secret-key
KAI_WAHA_HMAC_ALGORITHM=sha512

# Kokoro TTS (voice replies) — install with `kai vendors install kokoro`
KAI_WAHA_KOKORO_ENABLED=true
KAI_WAHA_KOKORO_VOICE=af_heart
# KAI_WAHA_KOKORO_LANG=en-us        # empty = derive from bot language
```

`KAI_WAHA_HMAC_KEY` is optional but strongly recommended for any non-loopback
bind. Generate one with:

```bash
openssl rand -hex 32
```

When HMAC is set, kAI verifies the `X-Webhook-Hmac` header against the raw body
using the configured algorithm (`sha256` or `sha512`, default `sha512`), and
registers the same algorithm with WAHA. Starting without an HMAC key on a
non-loopback bind emits a prominent warning.

## Text-to-Speech (Kokoro)

kAI can reply with voice notes using [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
via [`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx). The model runs
on ONNX Runtime (no PyTorch) in an isolated venv — it does not touch the
project's own dependencies.

### Setup

```bash
kai vendors install kokoro
```

This creates an isolated venv at `vendor/kokoro/` and downloads the int8
quantized model (~88MB) + voices (~27MB) into `models/kokoro/`.

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `KAI_WAHA_KOKORO_ENABLED` | `true` | Enable TTS voice replies. |
| `KAI_WAHA_KOKORO_VOICE` | `af_heart` | Voice name (must match the language). |
| `KAI_WAHA_KOKORO_LANG` | *(empty)* | Kokoro language code. Empty = derived from the bot's `language` setting. |
| `KAI_WAHA_KOKORO_SPEED` | `1.0` | Speech speed multiplier (0.5–2.0). |
| `KAI_WAHA_KOKORO_MODEL_PATH` | `models/kokoro/kokoro-v1.0.int8.onnx` | Path to the ONNX model (used by the cockpit's shared kokoro server). |
| `KAI_WAHA_KOKORO_VOICES_PATH` | `models/kokoro/voices-v1.0.bin` | Path to the voices file (used by the cockpit's shared kokoro server). |
| `KAI_WAHA_KOKORO_SERVER_HOST` | `127.0.0.1` | Shared kokoro server host the bot connects to as a client. |
| `KAI_WAHA_KOKORO_SERVER_PORT` | `8788` | Shared kokoro server port the bot connects to as a client. |

The default voice `af_heart` is an American English female voice, matching the
bot's default `language: English`. When you change the bot language, pick a
voice that matches — see [VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)
for the full list. Common pairs:

| Language | Voice example | Lang code |
|----------|---------------|-----------|
| English (US) | `af_heart`, `am_adam` | `en-us` |
| Spanish | `ef_dora`, `em_alex` | `es` |
| French | `ff_siwis` | `fr-fr` |
| Italian | `if_sara`, `im_nicola` | `it` |
| Portuguese (BR) | `pf_dora`, `pm_alex` | `pt-br` |

At startup, kAI checks that the venv, model, and voices are present and that
`kokoro_onnx` imports cleanly. If anything is missing, TTS is disabled with a
warning — the bot still runs, replying with text only.

### Per-Reply Language Detection

kAI matches the incoming message's language per turn (see the persona's
Language rule), so a single chat can move between languages over time. Voice
notes detect the reply's own language independently of the deployment's
static `language` setting, so the voice matches whatever language *that*
reply is actually written in, not just the bot's configured default.

For a reply with no detectable signal (too short/ambiguous for script or
stopword matching — an ack like "OK!" or "Listo."), kAI falls back to the
same chat's own last confidently-detected voice language rather than the
deployment's static default: a short ack in an otherwise-Spanish chat stays
Spanish instead of reverting to English just because that one reply had no
Spanish stopwords in it. The static `language`/`KAI_WAHA_KOKORO_LANG` is only
used as the very first fallback for a chat with no voice history yet.

## Bot Configuration

Config is loaded from `configs/waha.json` (relative to the working
directory, configurable via `KAI_CONFIGS_DIR`). There is no packaged
fallback — if it's missing, the bot runs on `BotConfig()`'s own field
defaults (see `src/kai/bots/waha/__init__.py`), not a duplicate config file.
Put your deployment-specific settings — whitelists, language, participation
tuning — in `configs/waha.json` so they live outside package source and
aren't overwritten on updates.

`configs/waha.json`:

```json
{
    "trigger_keyword": "kai",
    "whitelist": [],
    "blacklist": [],
    "language": "English",
    "timezone": "Europe/Berlin",
    "mentions_enabled": true,
    "media": {
        "image_enabled": true,
        "voice_enabled": true,
        "max_size_mb": 10
    },
    "participation": {
        "enabled": true,
        "rate": 0.15,
        "cooldown_seconds": 90,
        "streak_max": 2
    }
}
```

| Field | Description |
|-------|-------------|
| `trigger_keyword` | Word that summons kAI in groups (default `kai`). Empty = respond to all group messages. |
| `whitelist` / `blacklist` | Chat IDs and group authors to allow/block. `blacklist` wins. |
| `language` | Default reply language; overridable per-start with `--language`. |
| `timezone` | IANA timezone (e.g. `America/Santo_Domingo`) the bot tells the model for "current time". Defaults to the server's local timezone (often UTC in containers). |
| `mentions_enabled` | Resolve `@[Name]` into real WhatsApp mentions in groups. |
| `media` | Enable image understanding and voice transcription, set max media size. |
| `participation` | Organic (non-summoned) group participation — see below. |

Supported WhatsApp identifiers include `@c.us`, `@g.us`, and `@lid`.

### Prompt

`prompt.md` is the kAI persona, loaded with `{{language}}` substituted from
config. Every turn returns a structured `WahaAction` (see `actions.py`), not
free text — kAI's decision is the typed `action` field
(`reply`/`silent`/`sleep`/`send_dm`/`send_to_group`/`send_voice_note`/
`console`), not a string token embedded in the reply. Which values are even
offered to the model depends on the turn: a DM or a hard direct address never
offers `silent` at all (see `action_cls_for_turn`), so kAI structurally cannot
ghost someone who addressed him directly — he can still decide the content is
"nothing to add" and send a short acknowledgment, but the schema itself
removes the silent option.

## When kAI Speaks

kAI is a participant, not a spectator.

**Always replies:**

- Someone tags him, uses his name, or the trigger keyword.
- Someone replies to one of his messages (he then *decides* whether to respond
  — he may still choose `silent`).
- A direct (1-to-1) message — DMs never go silent (the `silent` action isn't
  offered at all on that turn).
- He was just woken from sleep.
- Safety-critical messages (crisis, self-harm, danger) — always.

**May chime in (organic participation):** in groups, kAI may offer himself a
chance to speak on messages not aimed at him. This is probabilistic with
guards so he never dominates a chat. See the `participation` config above.

**Stays silent (`action: "silent"`)** when the message isn't aimed at him and
there's nothing worth adding (throwaway messages, mid-thought bursts, fast
chat, hostile bait), and only on turns where `silent` is actually offered.

### Organic Participation

When `participation.enabled` is true, a non-summoned group message is offered
to the model with a chance to chime in. The model may still decline via the
`silent` action. Guards:

- `rate` — probability a given message is offered (default `0.15`), raised by
  ~0.2 when the message contains a question mark.
- `cooldown_seconds` — minimum gap between kAI's replies in a chat (default
  `90`). Messages arriving inside the cooldown are observed but never offered.
- `streak_max` — max consecutive organic replies before kAI forces a pause
  (default `2`). The streak decays as the chat moves on without him.

**Active exchange:** when kAI's last turn in a chat was a reply (the reply
streak is still active), a quick human follow-up is treated as a genuine
continuation rather than kAI dominating. In that state the cooldown is relaxed
to ~30% of its configured value and the offer rate gets an extra boost, so
back-and-forth isn't silenced mid-conversation. The `streak_max` cap still
applies, so kAI can't machine-gun a fast chat.

Set `rate: 0` (or `enabled: false`) to disable organic participation and keep
summon-only behavior.

## Sleep and Wake

kAI can be told to go quiet per chat. Both commands require his trigger
keyword (his name) so casual phrases like "goodnight everyone" or "I couldn't
sleep" never silence him.

- **Sleep:** `kai go to sleep`, `goodnight kai`, `kai shush`, `kai be quiet`,
  `kai quiet down`, `kai stop talking` → the model returns the `sleep` action,
  kAI stops speaking in that chat entirely (even if @-tagged) and sends a
  brief acknowledgment (`text`, or a default ack if omitted). Messages are
  still observed so he has context when woken.
- **Wake:** `wake up kai`, `kai wake up`, `kai rise and shine` → a direct
  address while asleep gives the model one chance to decide whether this is a
  genuine wake-up (any reply-shaped action wakes the chat; `silent`/`sleep`/
  `console` keeps it asleep). A just-woken kAI always gets offered a real
  reply action (never silence) on that same turn.
- The cockpit's `/sleep` and `/wake` routes (`on_sleep`/`on_wake` on the
  webhook) let an operator force either state directly, independent of chat
  content.

## Mentions

In group chats, kAI keeps a roster of participants seen since startup. If the
model replies with `@[Name]`, kAI resolves the name to a known participant and
sends a WhatsApp mention payload.

- On every inbound group message (rate-limited per chat) kAI fetches the full
  participant list from WAHA. This canonicalizes roster entries to the `@c.us`
  phone form (so mentions *send* a valid JID even when a member surfaces as an
  opaque `@lid`), records admins/superadmins for the per-chat prompt, and prunes
  members who have left the group.
- Display names still come from inbound messages (`notifyName`) — the
  participants endpoint returns only JIDs and roles, no names. Members who have
  never spoken can't be `@[Name]`-tagged (there's no name to match); use
  `/api/{session}/contacts/{id}` to resolve a lurker's name if needed.
- Matching is case- and accent-insensitive, Unicode-aware (Cyrillic, Arabic,
  CJK, etc. resolve correctly).
- Bare `@Name` mentions are also resolved for single-word names.
- If two participants share the same name, the mention is left as plain text
  rather than guessing — no silent wrong-mention.
- Disable with `"mentions_enabled": false`.

## Multimedia (Optional)

To enable image understanding and voice note transcription:

```bash
kai vendors install all
```

This downloads pre-built `ffmpeg` and builds `whisper.cpp` (static, via cmake)
plus the whisper base model into `vendor/` and `models/`.
Voice notes are transcribed locally with `whisper.cpp` — no external API calls.

Control media behavior via the `media` section of your config (see above).

## Operator Console

The bot exposes a `/tell` HTTP route (see the root README's "Operating A
Running Bot"). kAI expresses delivery through the same structured
`WahaAction` used for inbound turns:

- To answer the operator only, kAI returns `console` with `text` — this goes
  to the operator's console, never to WhatsApp.
- To deliver a message, kAI returns `send_dm` or `send_to_group` with both
  `target` (the exact chat JID from the instruction) and `text` filled in.
  There is no separate "send" tool and no `to` field on the request — the
  target always comes from what the agent read in the operator's own words.
- Pass `persist=true` to give the turn the `set_goal` tool, letting the
  operator permanize a steering directive into the system prompt.

## Reliability

- **Webhook idempotency:** duplicate webhook deliveries (same message ID) are
  detected and ignored, so WAHA retries never produce duplicate replies.
- **`@lid` send parsing:** WAHA accepts sends to opaque `@lid` targets (HTTP
  200, message delivered) but sometimes returns an empty or non-JSON body —
  a wwebjs serialization gap for the LID scheme. The client treats a 2xx with
  an empty/unparseable body as success rather than raising, so a real send
  isn't reported as a failure.
- **HMAC verification:** see Environment above.
- **Per-chat ordering:** messages in the same chat are processed and replied
  to in arrival order (guarded by a per-chat `asyncio.Lock`).

## CLI

```bash
uv run kai start waha [--goal TEXT] [--language TEXT]
uv run kai status waha
```

`start` loads config + prompt, connects to WAHA, registers the webhook, and
serves until interrupted. `--language` overrides the configured language
regardless of its value. `status` reports the WAHA session and account.
