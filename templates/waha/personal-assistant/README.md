# kAI Assistant

Full-featured personal assistant on WhatsApp. Schedules reminders and meetings, manages email, researches topics, and handles day-to-day tasks.

## Transport

Waha

## Actions

reply, silent, sleep, send_dm, console

## Tools

**Mandatory (always on):** escalate, blacklist, calculate

**Optional:** brain_query, sql_query, send_email, calcom, web_search, get_webpage_content, get_time_in_timezone, get_weather

**Bot-internal (always wired, not shown in settings):** record_note, get_conversation_messages, get_whatsapp_history, schedule_task, list_tasks, cancel_task

## Required environment

- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY
- **sql_query**: KAI_SQL_DSN
- **send_email**: KAI_SMTP_TOOL_HOST, KAI_SMTP_TOOL_USERNAME, KAI_SMTP_TOOL_PASSWORD, KAI_SMTP_TOOL_FROM_ADDRESS
- **calcom**: KAI_CALCOM_API_KEY

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
