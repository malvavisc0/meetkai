# kAI Lead Nurture

Email drip campaign assistant. Sends personalized follow-ups based on lead stage, nurtures warm leads, and re-engages cold ones — all via email.

## Transport

Email

## Actions

reply, silent, console

## Tools

**Required:** schedule_task

**Optional:** brain_query, send_email, list_tasks, cancel_task, record_note, get_conversation_messages, escalate

## Required environment

- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY
- **send_email**: KAI_SMTP_TOOL_HOST, KAI_SMTP_TOOL_USERNAME, KAI_SMTP_TOOL_PASSWORD, KAI_SMTP_TOOL_FROM_ADDRESS

## Escalation rules

- **high**: Lead shows clear buying intent, asks about pricing, or wants a meeting - Hot lead — ready for human follow-up
- **medium**: Lead asks a detailed product question outside the knowledge base - Unanswerable question — needs human review
- **medium**: Lead expresses frustration or asks to stop being contacted by a bot - Lead frustration — needs human attention

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
