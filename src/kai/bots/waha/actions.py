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

# The per-turn subsets are derived from a template's base action set (see
# ``action_cls_for_turn``), not referenced as module-level constants. The names
# below are only the canonical tuples used to seed the memoized class cache so
# the four named classes below stay singletons.
_NO_SILENT_ACTION_NAMES = tuple(a for a in _FULL_ACTION_NAMES if a != "silent")
_NO_VOICE_ACTION_NAMES = tuple(a for a in _FULL_ACTION_NAMES if a != "send_voice_note")
_NO_SILENT_NO_VOICE_ACTION_NAMES = tuple(
    a for a in _NO_SILENT_ACTION_NAMES if a != "send_voice_note"
)

# Type aliases for backward compatibility (used in cast() in tests).
_FULL_ACTIONS = Literal[
    "reply", "send_voice_note", "silent", "sleep", "send_dm", "send_to_group", "console"
]
_NO_SILENT_ACTIONS = Literal[
    "reply", "send_voice_note", "sleep", "send_dm", "send_to_group", "console"
]
_NO_VOICE_ACTIONS = Literal["reply", "silent", "sleep", "send_dm", "send_to_group", "console"]
_NO_SILENT_NO_VOICE_ACTIONS = Literal["reply", "sleep", "send_dm", "send_to_group", "console"]

# One ActionResult subclass per unique action tuple. Memoizing by tuple means
# ``action_cls_for_turn`` derives the per-turn vocabulary from a template's
# ``base_actions`` on demand AND identical combos return the same class object
# (so ``is``-identity assertions in tests/scripts hold for the canonical sets).
_ACTION_CLS_CACHE: dict[tuple[str, ...], type[ActionResult]] = {}


def build_action_cls(
    actions: tuple[str, ...],
    *,
    name: str = "TemplateAction",
    doc: str = "A template-driven action vocabulary.",
) -> type[ActionResult]:
    """Return the (cached) ActionResult subclass for ``actions``.

    The first caller for a given tuple names the class; later callers for the
    same tuple get the cached class regardless of the ``name`` they pass.
    """
    key = tuple(actions)
    cls = _ACTION_CLS_CACHE.get(key)
    if cls is None:
        cls = _make_action_cls(name, doc, key)
        _ACTION_CLS_CACHE[key] = cls
    return cls


WahaAction = build_action_cls(
    _FULL_ACTION_NAMES,
    name="WahaAction",
    doc="The waha bot's full action vocabulary (silence and voice allowed).",
)

WahaNoSilentAction = build_action_cls(
    _NO_SILENT_ACTION_NAMES,
    name="WahaNoSilentAction",
    doc="Waha action vocabulary that excludes silent. Used when user must not be ghosted.",
)

WahaNoVoiceAction = build_action_cls(
    _NO_VOICE_ACTION_NAMES,
    name="WahaNoVoiceAction",
    doc="Waha action vocabulary that excludes send_voice_note. Used when TTS is offline.",
)

WahaNoSilentNoVoiceAction = build_action_cls(
    _NO_SILENT_NO_VOICE_ACTION_NAMES,
    name="WahaNoSilentNoVoiceAction",
    doc="Waha action vocabulary excluding both silent and send_voice_note.",
)


def action_cls_for_turn(
    *,
    base_actions: tuple[str, ...],
    allow_silence: bool,
    operator: bool = False,
    tts_available: bool = True,
) -> type[ActionResult]:
    """Pick the ``output_cls`` for a waha turn, derived from ``base_actions``.

    ``base_actions`` is the template's declared action vocabulary (the full set
    the bot is taught). Per-turn context narrows it: a turn that must never go
    silent (DM / hard direct address) drops ``silent``; a turn with TTS offline
    drops ``send_voice_note``. The narrowing regenerates the class on demand
    from the template's base set rather than selecting among module-level
    constants, so a template that omits e.g. ``send_voice_note`` never offers it
    regardless of TTS availability.

    ``operator`` is reserved for ``tell`` turns (the ``console`` value is
    meaningful there); for inbound turns it is False. On an operator turn
    ``silent`` is kept even when ``allow_silence`` is True — the operator can
    always decline — matching the previous behavior.
    """
    actions = list(base_actions)
    if not allow_silence and not operator:
        actions = [a for a in actions if a != "silent"]
    if not tts_available:
        actions = [a for a in actions if a != "send_voice_note"]
    return build_action_cls(tuple(actions))
