# Kai Leads

Qualifies inbound WhatsApp leads. Asks discovery questions one at a time (BANT), schedules follow-ups for warm leads via the task scheduler, and escalates hot leads to the operator immediately.

## Transport

Waha

## Actions

reply, silent, sleep, send_dm, console

## Tools

**Required:** escalate, schedule_task

**Optional:** list_tasks, cancel_task, brain_query, sql_query, calcom, send_email, record_note, get_conversation_messages

## Required environment

- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY
- **sql_query**: KAI_SQL_DSN
- **calcom**: KAI_CALCOM_API_KEY
- **send_email**: KAI_SMTP_TOOL_HOST, KAI_SMTP_TOOL_USERNAME, KAI_SMTP_TOOL_PASSWORD, KAI_SMTP_TOOL_FROM_ADDRESS

## Escalation rules

- **high**: Lead is hot — clear budget, authority, and near-term timeline - Hot lead — operator should take over
- **high**: Lead asks to speak to a human or sales rep - Lead wants a human
- **critical**: Lead mentions a large deal or enterprise interest - High-value lead — immediate attention required

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
