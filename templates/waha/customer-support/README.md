# kAI Support

Knowledge-grounded WhatsApp support agent. Answers from the Brain when available, stays concise, tracks conversation state, and escalates to a human operator when it cannot answer or the customer is frustrated.

## Transport

Waha

## Actions

reply, silent, console

## Tools

**Required:** escalate, brain_query, get_conversation_messages, record_note

**Optional:** schedule_task, list_tasks, cancel_task, sql_query, send_email, calcom

## Required environment

- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY
- **sql_query**: KAI_SQL_DSN
- **send_email**: KAI_SMTP_TOOL_HOST, KAI_SMTP_TOOL_USERNAME, KAI_SMTP_TOOL_PASSWORD, KAI_SMTP_TOOL_FROM_ADDRESS
- **calcom**: KAI_CALCOM_API_KEY

## Escalation rules

- **high**: Customer explicitly asks for a human or to speak to someone - Customer wants a human agent
- **medium**: The question cannot be answered from the knowledge base - Unanswerable question — needs human review
- **critical**: Customer mentions refunds, legal action, or formal complaints - High-value complaint — immediate attention required

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
