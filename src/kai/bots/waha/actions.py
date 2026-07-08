"""waha's action vocabulary.

This is the bot-owned ``ActionResult`` subclass passed as ``output_cls`` to
every ``agent.chat()`` turn. It declares the closed set of actions the waha
prompt is taught to choose from and the parameters each needs. There is no
string protocol (``<<silent>>`` / ``<<sleep>>``) anywhere — dispatch is a
lookup table on ``action.action``, never a token scan.

- ``reply``          — deliver ``text`` to the conversation the turn came from.
- ``send_voice_note`` — deliver ``text`` as a synthesized WhatsApp voice note
                       (TTS) to the conversation the turn came from.
- ``silent``         — deliver nothing (replaces ``<<silent>>``).
- ``sleep``          — deliver ``text`` (or a default ack) as a goodbye, then
                       set the chat's sleep state (replaces ``<<sleep>>``).
- ``send_dm`` / ``send_to_group`` — deliver ``text`` to ``target`` instead of
                       the origin conversation.
- ``console``        — operator (``tell``) turns only: don't deliver anywhere,
                       just return ``text`` in the ``/tell`` HTTP response.

A turn that must never go silent (DM / hard direct address) is expressed by
*not offering* ``silent`` in the ``Literal`` for that turn — see
:func:`action_cls_for_turn`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from kai.agent.core import ActionResult

_TEXT_FIELD_DESC = (
    "The message content to deliver. Required for reply / "
    "send_voice_note / sleep / send_dm / send_to_group / console. "
    "For send_dm and send_to_group this is the exact text sent to the "
    "target chat."
)

_TARGET_FIELD_DESC = (
    "Destination chat JID for send_dm / send_to_group / "
    "send_voice_note (when delivering to a specific chat on an "
    "operator turn). Groups end in @g.us, DMs in @c.us. Must be "
    "set whenever action is send_dm or send_to_group. Leave empty "
    "for send_voice_note on an inbound turn (delivers to the origin "
    "chat). Unused for reply / silent / sleep / console."
)


def _make_action_cls(
    name: str,
    doc: str,
    actions: tuple[str, ...],
) -> type[ActionResult]:
    """Build an ActionResult subclass with a constrained ``action`` Literal."""
    action_literal = Literal[actions]  # type: ignore[valid-type]
    namespace = {
        "__annotations__": {
            "action": action_literal,
            "text": str | None,
            "target": str | None,
        },
        "text": Field(default=None, description=_TEXT_FIELD_DESC),
        "target": Field(default=None, description=_TARGET_FIELD_DESC),
        "__doc__": doc,
        "__module__": __name__,
    }
    return type(name, (ActionResult,), namespace)


_FULL_ACTION_NAMES = (
    "reply",
    "send_voice_note",
    "silent",
    "sleep",
    "send_dm",
    "send_to_group",
    "console",
)
_NO_SILENT_ACTION_NAMES = (
    "reply",
    "send_voice_note",
    "sleep",
    "send_dm",
    "send_to_group",
    "console",
)
_NO_VOICE_ACTION_NAMES = ("reply", "silent", "sleep", "send_dm", "send_to_group", "console")
_NO_SILENT_NO_VOICE_ACTION_NAMES = ("reply", "sleep", "send_dm", "send_to_group", "console")

# Type aliases for backward compatibility (used in cast() in tests).
_FULL_ACTIONS = Literal[
    "reply", "send_voice_note", "silent", "sleep", "send_dm", "send_to_group", "console"
]
_NO_SILENT_ACTIONS = Literal[
    "reply", "send_voice_note", "sleep", "send_dm", "send_to_group", "console"
]
_NO_VOICE_ACTIONS = Literal["reply", "silent", "sleep", "send_dm", "send_to_group", "console"]
_NO_SILENT_NO_VOICE_ACTIONS = Literal["reply", "sleep", "send_dm", "send_to_group", "console"]

WahaAction = _make_action_cls(
    "WahaAction",
    "The waha bot's full action vocabulary (silence and voice allowed).",
    _FULL_ACTION_NAMES,
)

WahaNoSilentAction = _make_action_cls(
    "WahaNoSilentAction",
    "Waha action vocabulary that excludes silent. Used when user must not be ghosted.",
    _NO_SILENT_ACTION_NAMES,
)

WahaNoVoiceAction = _make_action_cls(
    "WahaNoVoiceAction",
    "Waha action vocabulary that excludes send_voice_note. Used when TTS is offline.",
    _NO_VOICE_ACTION_NAMES,
)

WahaNoSilentNoVoiceAction = _make_action_cls(
    "WahaNoSilentNoVoiceAction",
    "Waha action vocabulary excluding both silent and send_voice_note.",
    _NO_SILENT_NO_VOICE_ACTION_NAMES,
)


def action_cls_for_turn(
    *,
    allow_silence: bool,
    operator: bool = False,
    tts_available: bool = True,
) -> type[ActionResult]:
    """Pick the ``output_cls`` for a waha turn.

    ``allow_silence`` collapses the old runtime flag into a schema decision:
    a turn that must never go silent simply omits ``silent`` from the
    reachable ``Literal``. ``operator`` is reserved for ``tell`` turns (the
    ``console`` value is meaningful there); for inbound turns it is False.
    ``tts_available`` omits ``send_voice_note`` when voice synthesis is
    offline — a capability alters the schema rather than remaining
    prompt-only advisory text. The four combinations of the two boolean
    gates map to the four action vocabularies above.
    """
    if not tts_available:
        return WahaNoSilentNoVoiceAction if not allow_silence else WahaNoVoiceAction
    return WahaAction if allow_silence or operator else WahaNoSilentAction
