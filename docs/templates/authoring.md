# Template Authoring Guide

This guide covers how to create and maintain templates for kAI bots.

## Directory layout

```
templates/<transport>/<name>/
  template.yaml    # Schema-driven template metadata, tools, config
  prompt.md        # System prompt with {{language}} and {{display_name}} substitutions
  README.md        # Human-readable description (shown in the cockpit)
```

Transport is `waha` (WhatsApp) or `email`. `name` is a kebab-case slug.

## template.yaml schema

All fields map to `kai.templates.schema.TemplateDef`:

| Field | Required | Type | Description |
|---|---|---|---|
| `name` | yes | `str` | Template slug, matches directory name |
| `transport` | yes | `"waha" \| "email"` | Transport this template targets |
| `display_name` | yes | `str` | Operator-facing name shown in the cockpit |
| `description` | yes | `str` | One-line purpose |
| `actions` | yes | `list[str]` | Action vocabulary (see below) |
| `config` | no | `dict` | Default config values (temperature, trigger_keyword, etc.) |
| `tools` | no | `object` | `required` and `optional` tool lists |
| `tools.required` | no | `list[str]` | Tools that must be available; boot fails if env is unset |
| `tools.optional` | no | `list[str]` | Tools loaded only when env vars are configured |
| `web_workflow` | no | `bool` | Default `true`; enables the web-based operator console |
| `post_processing` | no | `object` | Profile + step fields (see dead-config rule below) |
| `reply_style` | no | `str` | Style guidance appended to the system prompt |
| `goal_suggestion` | no | `str` | Suggested goal shown in the wizard |
| `escalation_rules` | no | `list[object]` | Rules for when to escalate to a human |
| `min_version` | no | `str` | Minimum kAI version needed |

### Escalation rule schema

```yaml
escalation_rules:
  - condition: "Customer is angry or uses profanity"
    severity: "high"
    message: "Escalate to a human agent"
```

Severity values: `low`, `medium`, `high`, `critical`.

### Dead-config rule for post_processing

If `post_processing.profile` is `waha_default` or `none`, the step fields
(`strip_emojis`, `strip_markdown`, `collapse_to_single_line`,
`strip_trailing_period`, `max_sentences`, `max_words`) are **ignored**.
Set `profile: custom` to use them. Attempting to set step fields with a
non-custom profile raises a validation error at load time.

## Action vocabulary per transport

**WAHA:** `reply`, `send_voice_note`, `silent`, `sleep`, `send_dm`, `send_to_group`, `console`

**Email:** `reply`, `silent`, `console`

Actions not valid for the transport cause the bot to fail at boot with a clear error.

## Tool environment requirements

| Tool | Required env vars |
|---|---|
| `brain_query` | `KAI_BRAIN_BASE_URL`, `KAI_BRAIN_LIGHTRAG_API_KEY` |
| `sql_query` | `KAI_SQL_DSN` |
| `send_email` | `KAI_SMTP_TOOL_HOST`, `KAI_SMTP_TOOL_USERNAME`, `KAI_SMTP_TOOL_PASSWORD`, `KAI_SMTP_TOOL_FROM_ADDRESS` |
| `calcom` | `KAI_CALCOM_API_KEY` |

A tool is "configured" only when **all** listed env vars are set. Tools in
`template.tools.required` must be configured; otherwise the bot fails to start.
Tools in `template.tools.optional` load only when configured.

The full set of known tool names is defined in `kai.templates.resolver._KNOWN_TOOL_NAMES`.
New tools must be added there before they can be referenced in a template.

## Prompt authoring

Use these variables for dynamic substitution:

- `{{language}}` — the operator's chosen language
- `{{display_name}}` — the bot's display name

### Tips

- Be explicit about when to use each action. Include few-shot examples for
  the primary actions.
- Define the persona, tone, and boundaries clearly.
- Reference tools by name with usage guidance.
- If `escalation_rules` are defined, include them in the prompt as hard rules.
- Keep the prompt concise. Long prompts increase latency and cost.

## Preview and test loop

```bash
# Preview the filled prompt without starting a bot
kai templates render waha/customer-support

# Start a bot with the template
kai start waha --template customer-support --goal "test"
```

The cockpit also provides a preview endpoint at `/deployments/new/preview?bot_type=waha&template=customer-support`.

## Config merge precedence

Config values resolve in this order (later overrides earlier):

1. CLI flags (e.g. `--temperature 0.5`)
2. `config.json` on disk
3. `template.yaml` defaults
4. System defaults (`BotConfig` defaults)

Example:

```
CLI:          temperature = 0.5
config.json:  language = "Spanish"
template:     temperature = 0.3, trigger_keyword = "kai"
system:       temperature = 0.4

Final: temperature = 0.5 (CLI), language = "Spanish" (config.json),
       trigger_keyword = "kai" (template)
```