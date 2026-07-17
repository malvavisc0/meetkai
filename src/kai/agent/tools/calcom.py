"""Cal.com tools — list event types, find slots, book, reschedule, and cancel.

Bot-agnostic: ``cli/bot.py:_start()`` registers these via
:func:`register_calcom_tool` when ``calcom_enabled`` is true. The
``api_key`` and ``base_url`` are closed over from the deployment's env.

Cal.com's v2 API pins a ``cal-api-version`` header per endpoint; each tool
pins its own (see ``_EVENT_TYPES_VERSION``, ``_SLOTS_VERSION``,
``_BOOKINGS_VERSION``). The workflow instruction composes alongside other
workflow blocks (web-search, Brain's, SQL's).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx
from llama_index.core.tools import FunctionTool
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The default Cal.com v2 API host. Mirrors the same constant in
# ``cockpit/calcom_connections.py`` (kept separate to avoid a cockpit↔agent
# layering import). ``base_url`` is optional in the catalog, so when the
# operator leaves it blank the cockpit does not inject KAI_CALCOM_BASE_URL
# and this default takes effect.
DEFAULT_BASE_URL = "https://api.cal.com/v2"

# Per-endpoint API versions — see module docstring.
_EVENT_TYPES_VERSION = "2024-06-14"
_SLOTS_VERSION = "2024-09-04"
_BOOKINGS_VERSION = "2026-02-25"

# Request timeout for every Cal.com call. Booking/slot lookups are fast;
# this caps a slow host rather than tuning per-endpoint.
_TIMEOUT = 30


class CalcomSettings(BaseSettings):
    """Cal.com tool settings — read from KAI_CALCOM_* env vars (injected by
    the cockpit)."""

    model_config = SettingsConfigDict(env_prefix="KAI_CALCOM_", env_file=".env", extra="ignore")

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    instruction: str = ""

    @property
    def calcom_enabled(self) -> bool:
        return bool(self.api_key)


def get_calcom_settings() -> CalcomSettings:
    return CalcomSettings()


class _CalcomToolAgent(Protocol):
    def register_tool(self, tool: FunctionTool) -> None: ...

    def set_tool_workflow(self, workflow: str | None) -> None: ...


def _headers(api_key: str, version: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "cal-api-version": version}


def _request(
    method: str,
    path: str,
    *,
    api_key: str,
    base_url: str,
    version: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[Any, str | None]:
    """Issue a Cal.com v2 request. Returns ``(parsed_json, None)`` on
    success or ``(None, error_message)`` on any failure (network error or a
    4xx/5xx response). The caller wraps the error message into the tool's
    ``"Error: ..."`` result string."""
    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}{path}"
    try:
        resp = httpx.request(
            method,
            url,
            headers=_headers(api_key, version),
            params=params,
            json=json_body,
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result
        return None, f"could not reach Cal.com API: {exc}"
    if resp.status_code >= 400:
        return None, _error_detail(resp)
    try:
        return resp.json(), None
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result
        return None, f"Cal.com API returned a non-JSON response: {exc}"


# Cap on the error body surfaced to the model — long stack traces or HTML
# error pages bloat the context without helping the model recover, so keep
# the detail bounded. Cal.com 4xx bodies are short JSON error messages.
_ERROR_DETAIL_MAX = 300


def _error_detail(resp: httpx.Response) -> str:
    """Build a concise ``Cal.com API returned {code}: {detail}`` string.

    Tries to extract a human-readable message from a JSON error body (Cal.com
    uses ``{"error": {"message": ...}}`` or ``{"message": ...}``), falling
    back to a truncated text body. Never raises — a malformed body degrades
    to the bare status code rather than failing the tool call.
    """
    base = f"Cal.com API returned {resp.status_code}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON error body
        text = (resp.text or "").strip()
        if not text:
            return base
        return f"{base}: {text[:_ERROR_DETAIL_MAX]}"
    if isinstance(body, dict):
        # Cal.com error shape is {"error": {"message": ..., "code": ...}};
        # some endpoints use a top-level "message" instead.
        err = body.get("error")
        msg = (
            (err.get("message") if isinstance(err, dict) else None)
            or body.get("message")
            or (err.get("code") if isinstance(err, dict) else None)
        )
        if msg:
            return f"{base}: {str(msg)[:_ERROR_DETAIL_MAX]}"
    return base


def build_calcom_workflow_instruction(instruction: str) -> str:
    """Render the operator's Cal.com usage rules into a workflow prompt block.

    Empty instruction = a minimal default that just tells the agent the tools
    exist. Non-empty = the operator's free-text rules, one trigger per line,
    appended as guidance.
    """
    base = (
        "You have Cal.com tools to manage appointments on the operator's "
        "calendar.\n"
        "\n"
        "Booking flow:\n"
        "1. `list_event_types` — discover what can be booked. Each event type "
        "includes a `bookingFields` array; fields marked `required` are custom "
        "questions you MUST answer before booking (collect them from the "
        "user). The default name/email fields are handled by the tool itself — "
        "only fields with a `slug` need answers.\n"
        "2. `find_available_slots` — check when the operator is free for a "
        "chosen event type (pass the event type id and a YYYY-MM-DD date "
        "range). Returns slots grouped by date; an empty result means "
        "genuinely no availability that day.\n"
        "3. `book_appointment` — create the booking. Pass the event type id, "
        "an ISO 8601 UTC start time (e.g. 2026-07-20T09:00:00Z), the "
        "attendee's name/email/timezone, and — if the event type has required "
        "booking fields — their answers keyed by slug in `booking_fields`. "
        "Cal.com rejects bookings missing required fields with a 400. Returns "
        "the booking, including its `uid`.\n"
        "\n"
        "Managing existing bookings:\n"
        "- `reschedule_booking` — move a booking to a new start time. Pass "
        "the booking `uid` and the new UTC start time.\n"
        "- `cancel_booking` — cancel a booking by its `uid`.\n"
        "\n"
        "Always confirm the slot is available before booking. Time zones "
        "matter: slots are returned in the timezone you request, but "
        "`start_time` for booking/rescheduling must always be UTC."
    )
    triggers = [ln.strip() for ln in instruction.splitlines() if ln.strip()]
    if not triggers:
        return base
    body = "\n".join(f"- {ln}" for ln in triggers)
    return f"{base}\nUse it when:\n{body}"


def make_calcom_tools(api_key: str, base_url: str) -> list[FunctionTool]:
    """Build the Cal.com tools bound to the operator's credentials.

    ``api_key`` and ``base_url`` are closed over — the LLM cannot override
    either. Tool signatures expose only the booking-relevant arguments.
    """

    def list_event_types() -> str:
        """List the operator's bookable Cal.com event types.

        Returns a compact JSON array — each item has the event type's id,
        title, lengthInMinutes, slug, bookingUrl, hidden flag, and bookingFields
        (custom questions with slug, label, required, type, and options where
        present). Call this first to find the event type id and required booking
        fields you need before booking.

        No arguments.
        """
        payload, err = _request(
            "GET",
            "/event-types",
            api_key=api_key,
            base_url=base_url,
            version=_EVENT_TYPES_VERSION,
        )
        if err is not None:
            return f"Error: {err}"
        # Project to the fields the agent needs to identify and book an event
        # type. The full Cal.com item carries ~50 fields (locations, metadata,
        # recurrence rules) that bloat the result without helping booking.
        # bookingFields is included (compacted to slug/label/required/type) so
        # the agent knows which custom questions it must collect answers for
        # before booking — Cal.com rejects bookings missing required fields
        # with a 400. Only fields carrying a slug are relevant: the default
        # name/email fields are satisfied by the attendee object, not by
        # bookingFieldsResponses.
        items = payload.get("data", []) if isinstance(payload, dict) else []

        def _compact_fields(fields: list | None) -> list[dict]:
            out = []
            for bf in fields or []:
                if not isinstance(bf, dict) or not bf.get("slug"):
                    continue
                item: dict[str, Any] = {
                    "slug": bf.get("slug"),
                    "label": bf.get("label"),
                    "required": bf.get("required", False),
                    "type": bf.get("type"),
                }
                # options exist only on select/multiselect/checkbox/radio
                # fields — include them so the agent can present the valid
                # choices to the user. Omit for text/textarea/etc. to keep
                # the output compact.
                if "options" in bf:
                    item["options"] = bf["options"]
                out.append(item)
            return out

        compact = [
            {
                "id": et.get("id"),
                "title": et.get("title"),
                "slug": et.get("slug"),
                "lengthInMinutes": et.get("lengthInMinutes"),
                "bookingUrl": et.get("bookingUrl"),
                "hidden": et.get("hidden"),
                "bookingFields": _compact_fields(et.get("bookingFields")),
            }
            for et in items
            if isinstance(et, dict)
        ]
        return json.dumps(compact)

    def find_available_slots(
        event_type_id: int, start_date: str, end_date: str, timezone: str = "UTC"
    ) -> str:
        """Find available time slots for a Cal.com event type.

        Args:
            event_type_id: The event type id (from list_event_types).
            start_date: Start of the range as YYYY-MM-DD (UTC).
            end_date: End of the range as YYYY-MM-DD (UTC).
            timezone: IANA timezone for the returned slot times (e.g.
                Europe/Berlin). Defaults to UTC.
        """
        payload, err = _request(
            "GET",
            "/slots",
            api_key=api_key,
            base_url=base_url,
            version=_SLOTS_VERSION,
            params={
                "eventTypeId": event_type_id,
                "start": start_date,
                "end": end_date,
                "timeZone": timezone,
                "format": "range",
            },
        )
        if err is not None:
            return f"Error: {err}"
        # data is a {date: [{start, end}, ...]} map; empty object means no
        # slots. Return it verbatim — the structure is already compact.
        return json.dumps(payload.get("data", {}) if isinstance(payload, dict) else {})

    def book_appointment(
        event_type_id: int,
        start_time: str,
        attendee_name: str,
        attendee_email: str,
        timezone: str,
        booking_fields: dict[str, Any] | None = None,
    ) -> str:
        """Book a Cal.com appointment.

        Args:
            event_type_id: The event type id (from list_event_types).
            start_time: The booking start as an ISO 8601 UTC timestamp
                (e.g. 2026-07-20T09:00:00Z). Must be UTC.
            attendee_name: The attendee's full name.
            attendee_email: The attendee's email address.
            timezone: The attendee's IANA timezone (e.g. America/New_York).
            booking_fields: Optional responses to the event type's custom
                booking questions, keyed by field slug (from list_event_types'
                bookingFields). You MUST include every field marked required
                — Cal.com rejects bookings missing required fields with a 400.
        """
        body: dict[str, Any] = {
            "eventTypeId": event_type_id,
            "start": start_time,
            "attendee": {
                "name": attendee_name,
                "email": attendee_email,
                "timeZone": timezone,
            },
        }
        if booking_fields:
            body["bookingFieldsResponses"] = booking_fields
        payload, err = _request(
            "POST",
            "/bookings",
            api_key=api_key,
            base_url=base_url,
            version=_BOOKINGS_VERSION,
            json_body=body,
        )
        if err is not None:
            return f"Error: {err}"
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return "Error: Cal.com API returned an unexpected booking response"
        return json.dumps(data)

    def reschedule_booking(booking_uid: str, start_time: str, reason: str = "") -> str:
        """Reschedule a Cal.com booking to a new start time.

        Args:
            booking_uid: The booking's uid (from book_appointment or a prior
                reschedule).
            start_time: The new start time as an ISO 8601 UTC timestamp
                (e.g. 2026-07-20T09:00:00Z). Must be UTC.
            reason: Optional reason for rescheduling.
        """
        body: dict[str, Any] = {"start": start_time}
        if reason:
            body["rescheduleReason"] = reason
        payload, err = _request(
            "POST",
            f"/bookings/{booking_uid}/reschedule",
            api_key=api_key,
            base_url=base_url,
            version=_BOOKINGS_VERSION,
            json_body=body,
        )
        if err is not None:
            return f"Error: {err}"
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return "Error: Cal.com API returned an unexpected booking response"
        return json.dumps(data)

    def cancel_booking(booking_uid: str, reason: str = "") -> str:
        """Cancel a Cal.com booking.

        Args:
            booking_uid: The booking's uid (returned by book_appointment).
            reason: Optional cancellation reason.
        """
        body: dict[str, Any] = {"cancellationReason": reason}
        payload, err = _request(
            "POST",
            f"/bookings/{booking_uid}/cancel",
            api_key=api_key,
            base_url=base_url,
            version=_BOOKINGS_VERSION,
            json_body=body,
        )
        if err is not None:
            return f"Error: {err}"
        data = payload.get("data") if isinstance(payload, dict) else None
        return json.dumps(data) if data is not None else "cancelled"

    return [
        FunctionTool.from_defaults(
            fn=list_event_types,
            name="list_event_types",
            description=(
                "List the operator's bookable Cal.com event types (id, title, "
                "length, slug, booking URL). Call before booking to get the "
                "event type id."
            ),
        ),
        FunctionTool.from_defaults(
            fn=find_available_slots,
            name="find_available_slots",
            description=(
                "Find available time slots for a Cal.com event type between two "
                "dates. Pass the event type id and a YYYY-MM-DD start/end range."
            ),
        ),
        FunctionTool.from_defaults(
            fn=book_appointment,
            name="book_appointment",
            description=(
                "Book a Cal.com appointment. The start time must be an ISO 8601 "
                "UTC timestamp. If the event type has required booking fields "
                "(from list_event_types), pass their answers in booking_fields. "
                "Returns the booking including its uid."
            ),
        ),
        FunctionTool.from_defaults(
            fn=reschedule_booking,
            name="reschedule_booking",
            description=(
                "Move an existing Cal.com booking to a new start time. Pass the "
                "booking uid and the new ISO 8601 UTC start time."
            ),
        ),
        FunctionTool.from_defaults(
            fn=cancel_booking,
            name="cancel_booking",
            description="Cancel a Cal.com booking by its uid.",
        ),
    ]


def register_calcom_tool(
    agent: _CalcomToolAgent,
    *,
    api_key: str,
    base_url: str,
    instruction: str = "",
) -> None:
    """Register the Cal.com tools on agent and inject the workflow prompt.

    No persistent resource to return (httpx connections are short-lived).
    """
    for tool in make_calcom_tools(api_key, base_url):
        agent.register_tool(tool)
    agent.set_tool_workflow(build_calcom_workflow_instruction(instruction))
