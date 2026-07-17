# Kai Community Manager

WhatsApp group moderator. Enforces community rules, answers FAQs, mediates conflicts, and DMs members when a topic needs private attention.

## Transport

Waha

## Actions

reply, silent, send_dm, send_to_group, console

## Tools

**Required:** escalate

**Optional:** brain_query, web_search, record_note, get_conversation_messages, get_whatsapp_history, schedule_task, list_tasks, cancel_task

## Required environment

- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY

## Escalation rules

- **critical**: A member is harassing, threatening, or using hate speech - Harassment or hate speech — immediate intervention needed
- **high**: A member repeatedly violates group rules after warnings - Repeated rule violations — member may need to be removed
- **medium**: Someone asks a question you cannot answer from the knowledge base - Unanswerable question — needs human review
- **critical**: A member mentions legal action, self-harm, or safety concerns - Legal or safety concern — immediate human attention required

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
