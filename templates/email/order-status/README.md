# kAI Orders

Answers order-status emails by querying the order database. Returns tracking info, handles simple return requests, and escalates anything outside its scope.

## Transport

Email

## Actions

reply, silent, console

## Tools

**Required:** sql_query

**Optional:** brain_query, record_note, get_conversation_messages, escalate

## Required environment

- **sql_query**: KAI_SQL_DSN
- **brain_query**: KAI_BRAIN_BASE_URL, KAI_BRAIN_LIGHTRAG_API_KEY

## Quick start

Selectable from the deployment wizard; tool toggles on the settings page.
