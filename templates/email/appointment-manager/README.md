# kAI Bookings

Books, reschedules, and cancels appointments via Cal.com. Sends confirmations and handles scheduling requests over email.

## Transport

Email

## Actions

reply, silent, console

## Tools

**Required:** calcom, schedule_task

**Optional:** list_tasks, cancel_task, brain_query, record_note, get_conversation_messages, escalate, send_email

## Required environment

- **calcom**: KAI_CALCOM_API_KEY
- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY
- **send_email**: KAI_SMTP_TOOL_HOST, KAI_SMTP_TOOL_USERNAME, KAI_SMTP_TOOL_PASSWORD, KAI_SMTP_TOOL_FROM_ADDRESS

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
