"""Tests for the Cal.com agent tools + CalcomSettings."""

import inspect
import json
from unittest.mock import MagicMock, patch

from kai.agent.tools.calcom import (
    CalcomSettings,
    build_calcom_workflow_instruction,
    make_calcom_tools,
)


def _call_tool(tool, **kwargs) -> str:
    """Call a FunctionTool's underlying fn directly (no LLM round-trip)."""
    return tool.fn(**kwargs)


def _tools():
    return {t.metadata.name: t for t in make_calcom_tools("cal_live_key", "")}


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | list | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class TestSettings:
    def test_disabled_without_api_key(self, monkeypatch):
        monkeypatch.setenv("KAI_CALCOM_API_KEY", "")
        s = CalcomSettings()
        assert s.calcom_enabled is False

    def test_enabled_with_api_key(self, monkeypatch):
        monkeypatch.setenv("KAI_CALCOM_API_KEY", "cal_live_x")
        s = CalcomSettings()
        assert s.calcom_enabled is True

    def test_base_url_defaults_when_not_injected(self, monkeypatch):
        monkeypatch.delenv("KAI_CALCOM_BASE_URL", raising=False)
        monkeypatch.setenv("KAI_CALCOM_API_KEY", "cal_live_x")
        s = CalcomSettings()
        assert s.base_url == "https://api.cal.com/v2"


class TestToolSignature:
    def test_credentials_not_exposed(self):
        for tool in make_calcom_tools("cal_live_key", "https://custom/v2"):
            params = set(inspect.signature(tool.fn).parameters.keys())
            assert "api_key" not in params
            assert "base_url" not in params


class TestListEventTypes:
    def test_projects_compact_fields(self):
        tools = _tools()
        payload = {
            "status": "success",
            "data": [
                {
                    "id": 1,
                    "title": "Intro",
                    "slug": "intro",
                    "lengthInMinutes": 30,
                    "bookingUrl": "https://cal.com/bob/intro",
                    "hidden": False,
                    "bookingFields": [
                        {"type": "name", "label": "Name"},
                        {"type": "email", "label": "Email"},
                        {
                            "slug": "topic",
                            "type": "text",
                            "label": "What's this about?",
                            "required": True,
                        },
                        {
                            "slug": "service",
                            "type": "select",
                            "label": "Which service?",
                            "required": True,
                            "options": ["Consultation", "Demo", "Support"],
                            "hidden": False,
                        },
                        {
                            "slug": "notes",
                            "type": "textarea",
                            "label": "Notes",
                            "required": False,
                        },
                    ],
                    "metadata": {"also": "dropped"},
                }
            ],
        }
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(200, payload),
        ) as mock_req:
            result = _call_tool(tools["list_event_types"])
        items = json.loads(result)
        # Default name/email fields (no slug) are dropped — they're handled
        # by the attendee object. Custom fields with slugs are kept compact.
        assert items == [
            {
                "id": 1,
                "title": "Intro",
                "slug": "intro",
                "lengthInMinutes": 30,
                "bookingUrl": "https://cal.com/bob/intro",
                "hidden": False,
                "bookingFields": [
                    {
                        "slug": "topic",
                        "label": "What's this about?",
                        "required": True,
                        "type": "text",
                    },
                    {
                        "slug": "service",
                        "label": "Which service?",
                        "required": True,
                        "type": "select",
                        "options": ["Consultation", "Demo", "Support"],
                    },
                    {
                        "slug": "notes",
                        "label": "Notes",
                        "required": False,
                        "type": "textarea",
                    },
                ],
            }
        ]
        # Pins the event-types API version (not a global version).
        _, kwargs = mock_req.call_args
        assert kwargs["headers"]["cal-api-version"] == "2024-06-14"
        assert kwargs["headers"]["Authorization"] == "Bearer cal_live_key"


class TestFindAvailableSlots:
    def test_returns_data_map(self):
        tools = _tools()
        payload = {"status": "success", "data": {"2026-07-20": [{"start": "x", "end": "y"}]}}
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(200, payload),
        ) as mock_req:
            result = _call_tool(
                tools["find_available_slots"],
                event_type_id=10,
                start_date="2026-07-20",
                end_date="2026-07-21",
            )
        assert json.loads(result) == {"2026-07-20": [{"start": "x", "end": "y"}]}
        _, kwargs = mock_req.call_args
        # Slots pins a different version than event-types.
        assert kwargs["headers"]["cal-api-version"] == "2024-09-04"
        assert kwargs["params"]["format"] == "range"
        assert kwargs["params"]["eventTypeId"] == 10


class TestBookAppointment:
    def test_returns_booking_and_sends_body(self):
        tools = _tools()
        booking = {"uid": "uid-1", "title": "Intro", "status": "accepted", "start": "s", "end": "e"}
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(201, {"status": "success", "data": booking}),
        ) as mock_req:
            result = _call_tool(
                tools["book_appointment"],
                event_type_id=10,
                start_time="2026-07-20T09:00:00Z",
                attendee_name="Bob",
                attendee_email="bob@x.com",
                timezone="Europe/Berlin",
            )
        assert json.loads(result) == booking
        _, kwargs = mock_req.call_args
        assert kwargs["headers"]["cal-api-version"] == "2026-02-25"
        assert kwargs["json"] == {
            "eventTypeId": 10,
            "start": "2026-07-20T09:00:00Z",
            "attendee": {"name": "Bob", "email": "bob@x.com", "timeZone": "Europe/Berlin"},
        }

    def test_includes_booking_fields_responses(self):
        tools = _tools()
        booking = {"uid": "uid-2", "status": "accepted"}
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(201, {"status": "success", "data": booking}),
        ) as mock_req:
            _call_tool(
                tools["book_appointment"],
                event_type_id=10,
                start_time="2026-07-20T09:00:00Z",
                attendee_name="Bob",
                attendee_email="bob@x.com",
                timezone="UTC",
                booking_fields={"topic": "Project kickoff", "company_size": "10-50"},
            )
        _, kwargs = mock_req.call_args
        assert kwargs["json"]["bookingFieldsResponses"] == {
            "topic": "Project kickoff",
            "company_size": "10-50",
        }

    def test_no_booking_fields_omits_key(self):
        tools = _tools()
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(201, {"status": "success", "data": {"uid": "u"}}),
        ) as mock_req:
            _call_tool(
                tools["book_appointment"],
                event_type_id=10,
                start_time="2026-07-20T09:00:00Z",
                attendee_name="Bob",
                attendee_email="bob@x.com",
                timezone="UTC",
            )
        _, kwargs = mock_req.call_args
        assert "bookingFieldsResponses" not in kwargs["json"]


class TestRescheduleBooking:
    def test_posts_to_reschedule_path_with_version(self):
        tools = _tools()
        rescheduled = {"uid": "uid-2", "status": "accepted", "start": "new", "end": "new2"}
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(200, {"status": "success", "data": rescheduled}),
        ) as mock_req:
            result = _call_tool(
                tools["reschedule_booking"],
                booking_uid="uid-1",
                start_time="2026-07-21T10:00:00Z",
                reason="attendee requested a later time",
            )
        assert json.loads(result) == rescheduled
        (method, url), kwargs = mock_req.call_args
        assert method == "POST"
        assert url.endswith("/bookings/uid-1/reschedule")
        assert kwargs["headers"]["cal-api-version"] == "2026-02-25"
        assert kwargs["json"] == {
            "start": "2026-07-21T10:00:00Z",
            "rescheduleReason": "attendee requested a later time",
        }

    def test_no_reason_omits_reason_key(self):
        tools = _tools()
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(200, {"status": "success", "data": {"uid": "uid-2"}}),
        ) as mock_req:
            _call_tool(
                tools["reschedule_booking"],
                booking_uid="uid-1",
                start_time="2026-07-21T10:00:00Z",
            )
        _, kwargs = mock_req.call_args
        assert kwargs["json"] == {"start": "2026-07-21T10:00:00Z"}


class TestCancelBooking:
    def test_posts_to_cancel_path_with_version(self):
        tools = _tools()
        cancel_data = {"status": "success", "data": {"uid": "uid-1", "status": "cancelled"}}
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(200, cancel_data),
        ) as mock_req:
            result = _call_tool(
                tools["cancel_booking"], booking_uid="uid-1", reason="changed plans"
            )
        assert json.loads(result)["status"] == "cancelled"
        (method, url), kwargs = mock_req.call_args
        assert method == "POST"
        assert url.endswith("/bookings/uid-1/cancel")
        assert kwargs["headers"]["cal-api-version"] == "2026-02-25"
        assert kwargs["json"] == {"cancellationReason": "changed plans"}

    def test_no_reason_sends_empty_string(self):
        tools = _tools()
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(200, {"status": "success", "data": {"uid": "uid-1"}}),
        ) as mock_req:
            _call_tool(tools["cancel_booking"], booking_uid="uid-1")
        _, kwargs = mock_req.call_args
        assert kwargs["json"] == {"cancellationReason": ""}


class TestErrorPaths:
    def test_http_error_returns_error_string(self):
        tools = _tools()
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(401, {"error": {"message": "Invalid API key"}}),
        ):
            result = _call_tool(tools["list_event_types"])
        assert result.startswith("Error: Cal.com API returned 401: Invalid API key")

    def test_http_error_with_plain_message_field(self):
        tools = _tools()
        with patch(
            "kai.agent.tools.calcom.httpx.request",
            return_value=_FakeResp(409, {"message": "Slot is no longer available"}),
        ):
            result = _call_tool(
                tools["book_appointment"],
                event_type_id=1,
                start_time="2026-07-20T09:00:00Z",
                attendee_name="A",
                attendee_email="a@b.com",
                timezone="UTC",
            )
        assert "409" in result
        assert "Slot is no longer available" in result

    def test_http_error_non_json_falls_back_to_text(self):
        tools = _tools()
        resp = MagicMock()
        resp.status_code = 502
        resp.json.side_effect = ValueError("nope")
        resp.text = "<html>Bad Gateway</html>"
        with patch("kai.agent.tools.calcom.httpx.request", return_value=resp):
            result = _call_tool(tools["list_event_types"])
        assert "502" in result
        assert "Bad Gateway" in result

    def test_http_error_empty_body_returns_bare_status(self):
        tools = _tools()
        resp = MagicMock()
        resp.status_code = 500
        resp.json.side_effect = ValueError("nope")
        resp.text = ""
        with patch("kai.agent.tools.calcom.httpx.request", return_value=resp):
            result = _call_tool(tools["list_event_types"])
        assert result == "Error: Cal.com API returned 500"

    def test_network_error_returns_error_string(self):
        tools = _tools()
        import httpx as _httpx

        with patch(
            "kai.agent.tools.calcom.httpx.request",
            side_effect=_httpx.ConnectError("boom"),
        ):
            result = _call_tool(tools["list_event_types"])
        assert result.startswith("Error: could not reach Cal.com API")

    def test_non_json_response_returns_error_string(self):
        tools = _tools()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("nope")
        with patch("kai.agent.tools.calcom.httpx.request", return_value=resp):
            result = _call_tool(tools["list_event_types"])
        assert result.startswith("Error: Cal.com API returned a non-JSON response")


class TestWorkflowInstruction:
    def test_base_when_empty(self):
        out = build_calcom_workflow_instruction("")
        assert "Booking flow:" in out
        assert "list_event_types" in out
        assert "reschedule_booking" in out
        assert "bookingFields" in out
        assert "Use it when:" not in out

    def test_appends_triggers(self):
        out = build_calcom_workflow_instruction("someone asks to book\nsomeone wants to cancel")
        assert "Use it when:" in out
        assert "- someone asks to book" in out
        assert "- someone wants to cancel" in out
