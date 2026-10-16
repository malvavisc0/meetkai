"""Vulture whitelist — names vulture flags at min-confidence 60 that are
NOT dead code. Every entry is framework-dispatched or an intentional seam:

- typer commands and FastAPI route handlers are invoked by their frameworks,
  never called by name from our code;
- pydantic validators/factories (``model_config``, ``validate_*``,
  ``default_*``, ``coerce_*``, ``reject_dead_config``) are dispatched by
  pydantic;
- pydantic/SQLAlchemy/dataclass fields (``updated_at``, ``fulfilled_at``,
  ``error_msg``, ``icon``, ``help``, ``testable``, ``webhook_type``,
  ``min_version``, ``markdown_with_citations``, ``references_markdown``) are
  populated from external data and rendered by templates or the DB layer;
- ``should_exit`` / ``header_style`` configure uvicorn / rich objects;
- agent/bot plugin API (``observe``, ``unregister_tool``, ``clear_tools``,
  ``set_tool_call_callback``, ``clear_goal``, ``revision``,
  ``post_processing_profile``) is called by bot packages, not in-tree;
- documented test seams (``reset_logging``, ``clear_key_cache``,
  ``clear_seen_nonces``, ``clean_brain_env``) reset module state between
  tests; ``_template`` / ``_settings`` are stored for bot subclasses.

A vulture hit at 60 that is NOT in this file is a defect: fix the call site
or delete the code. Keep this list curated — do not add entries to silence
new hits without a comment-level justification like the above.
"""

# typer commands (dispatched by the typer app)
list_cmd
show_cmd
render_cmd
cockpit_serve
cockpit_migrate
cockpit_rotate_credential_key
cockpit_user_create
cockpit_user_list
cockpit_user_disable
cockpit_user_flags
cockpit_request_list
cockpit_request_create
cockpit_request_approve
connection_list
deployment_create
deployment_list
deployment_start
deployment_stop
deployment_edit
deployment_delete

# FastAPI routes / exception handlers (dispatched by FastAPI)
webhook
ingest
get_csrf_token
http_exception_handler
login_get
login_post
login_auth
logout
brains_page
brains_create
brains_update_instruction
brains_upload
brains_ingest_url
brains_ingest_text
brains_delete_document
chat_send
chat_clear
connections_page
calcom_page
calcom_save
calcom_delete
calcom_test
db_page
db_save
db_delete
db_test
resend_page
resend_save
resend_delete
resend_test
smtp_page
smtp_save
smtp_delete
webhook_ingest
index
dependencies_page
deployment_detail
deployment_history
deployment_restart
deployment_settings_page
deployment_settings
deploy_new_get
deploy_new_post
template_preview
dashboard
resolve_api

# pydantic-dispatched validators / factories / config
model_config
default_none
default_metadata
coerce_source
validate_url
validate_max_depth
validate_max_pages
set_cookie_secure_default
reject_dead_config
validate_store_folder_absolute
validate_llm_api_base
validate_agent_max_history_messages
validate_agent_max_history_chars

# pydantic / SQLAlchemy / dataclass fields populated from external data
updated_at
fulfilled_at
error_msg
icon
help
testable
webhook_type
min_version
markdown_with_citations
references_markdown

# framework object configuration (uvicorn / rich)
should_exit
header_style

# agent + bot plugin API (called by bot packages, not in-tree)
observe
unregister_tool
clear_tools
set_tool_call_callback
clear_goal
revision
post_processing_profile

# bot subclass storage
_template
_settings

# documented test seams (reset module state between tests)
reset_logging
clear_key_cache
clear_seen_nonces
clean_brain_env
