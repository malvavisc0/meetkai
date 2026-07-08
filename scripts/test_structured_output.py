"""Live structured-output smoke test (no WAHA server required).

Builds a ``KaiAgent`` from the project's ``.env`` / ``KAI_*`` env vars and
exercises the terminal structured-prediction step against the real LLM — the
exact path ``handle_operator`` uses. It verifies that the model's reply is
parsed into a properly-typed ``WahaAction`` (not a blank object) with
``action`` / ``text`` / ``target`` populated.

It also covers turns that combine tool calling with structured output:
- a web-search + summarize turn (World Cup 2026)
- an operator turn that calls ``get_hardware_info`` before replying
- an operator turn that calls ``get_weather`` then sends to a group

Run from the project root:

    .venv/bin/python scripts/test_structured_output.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from llama_index.core.tools import FunctionTool

from kai.agent.context import MessageContext
from kai.agent.core import ChatResult, KaiAgent
from kai.agent.tools import WEB_WORKFLOW_INSTRUCTIONS
from kai.bots.waha.actions import WahaAction
from kai.bots.waha.processing import REPLY_STYLE

# Mirror the operator-turn extra context from kai.bots.waha.Bot so the model
# sees the same instructions it would on a real /tell turn.
_OPERATOR_TURN_CONTEXT = (
    "You received an instruction from the operator (the person who runs "
    "you). You express your decision through the structured action object "
    "you return — there is no tool for sending messages.\n"
    "IMPORTANT: action values (send_to_group, send_dm, console, reply, "
    "send_voice_note, silent, sleep) are NOT tools. Never call them as "
    'functions. They are values for the "action" field in your JSON '
    "response.\n"
    "- To deliver a message to a WhatsApp chat, set action to "
    '"send_to_group" (for groups, @g.us) or "send_dm" (for DMs, '
    '@c.us). You MUST fill BOTH fields: "target" = the exact chat '
    "JID taken from the instruction (never invent or guess one), and "
    '"text" = the exact message to send (plain prose, no action tokens '
    "or field names in it). Returning a send action with an empty "
    '"target" or "text" is never correct — if the instruction gives you '
    "both, copy them verbatim into the fields.\n"
    "- To deliver a VOICE NOTE to a WhatsApp chat, set action to "
    '"send_voice_note" with "target" = the destination chat JID '
    '(same rules as send_to_group/send_dm) and "text" = the words to '
    "synthesize (plain prose, short). Use this when the instruction "
    "explicitly asks for a voice note.\n"
    "- To reply to the operator (answer a question, confirm a steering "
    'directive), set action to "console" and put your reply in "text".\n'
    "If the instruction is a steering directive and you have the "
    "set_goal tool, call it to permanize the goal."
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _operator_ctx() -> MessageContext:
    return MessageContext(
        sender_name="operator",
        sender_id="<operator>",
        addressed_to_bot=True,
    )


def _print_result(label: str, result: ChatResult) -> bool:
    action = result.action
    print(f"\n=== {label} ===")
    print(f"  ChatResult.error : {result.error!r}")
    print(f"  action type      : {type(action).__name__}")
    print(f"  action.action    : {action.action!r}")
    print(f"  action.text      : {action.text!r}")
    print(f"  action.target    : {getattr(action, 'target', '<no attr>')!r}")
    print(f"  is WahaAction    : {isinstance(action, WahaAction)}")
    if result.tool_calls:
        print(f"  tool_calls ({len(result.tool_calls)}):")
        for tc in result.tool_calls:
            status = "ok" if tc.ok else "FAIL"
            snippet = tc.result if len(tc.result) <= 80 else tc.result[:77] + "..."
            print(f"    [{status}] {tc.name}({tc.args}) -> {snippet}")
    else:
        print("  tool_calls       : (none)")
    return isinstance(action, WahaAction) and result.error is None


def _select_tools(agent: KaiAgent, names: list[str]) -> list[FunctionTool]:
    """Pick a subset of the agent's registered tools by name.

    Mirrors what ``handle_operator`` does with ``_operator_tools`` — only
    the tools relevant to the turn are exposed, so the model isn't tempted
    to emit action names as tool calls.
    """
    by_name = {t.metadata.name: t for t in agent.get_tools()}
    return [by_name[n] for n in names if n in by_name]


async def main() -> int:
    agent = KaiAgent(namespace="structured_output_test")
    agent.set_system_prompt(
        "You are Kai, a WhatsApp assistant. Follow the operator's "
        "instructions precisely and express every decision through the "
        "structured action object. Action values (reply, send_voice_note, "
        "silent, sleep, send_dm, send_to_group, console) are NOT tools — "
        "never call them as functions; set them in the JSON response."
    )
    agent.set_timezone("Europe/Berlin")
    agent.set_tool_workflow(WEB_WORKFLOW_INSTRUCTIONS)

    failures: list[str] = []

    # ------------------------------------------------------------------
    # Turn 1: operator send_to_group directive (the case that was returning
    # target=null before the fix). No tools needed — the model should emit
    # the action directly.
    # ------------------------------------------------------------------
    send_msg = "Send the message 'hola mundo' to the group 11235677890-1111111111@g.us"
    result = await agent.chat(
        send_msg,
        output_cls=WahaAction,
        conversation_id="operator",
        context=_operator_ctx(),
        tools=[],
        extra_system_context=_OPERATOR_TURN_CONTEXT,
        reply_style=REPLY_STYLE,
    )
    ok = _print_result("send_to_group turn", result)
    action = result.action
    if not ok:
        failures.append("send_to_group: not a typed WahaAction or errored")
    elif action.action not in ("send_to_group", "send_dm"):
        failures.append(
            f"send_to_group: expected action send_to_group/send_dm, got {action.action!r}"
        )
    elif not getattr(action, "target", None):
        failures.append("send_to_group: target is empty (the original bug)")
    elif "hola mundo" not in (action.text or "").lower():
        failures.append(f"send_to_group: text mismatch, got {action.text!r}")

    # ------------------------------------------------------------------
    # Turn 2: operator console reply (operator asked a question). No tools
    # needed — the model should answer directly.
    # ------------------------------------------------------------------
    result = await agent.chat(
        "What is 2+2? Reply to me on the console.",
        output_cls=WahaAction,
        conversation_id="operator",
        context=_operator_ctx(),
        tools=[],
        extra_system_context=_OPERATOR_TURN_CONTEXT,
        reply_style=REPLY_STYLE,
    )
    ok = _print_result("console turn", result)
    action = result.action
    if not ok:
        failures.append("console: not a typed WahaAction or errored")
    elif action.action != "console":
        failures.append(f"console: expected action 'console', got {action.action!r}")
    elif not action.text:
        failures.append("console: text is empty")

    # ------------------------------------------------------------------
    # Turn 2b: send_voice_note with a target. The model should pick the
    # ``send_voice_note`` action (not ``reply`` or ``send_to_group``)
    # because the instruction explicitly asks for a voice note, AND fill
    # ``target`` with the destination chat JID. No tools needed.
    # Verifies the LLM maps "send as voice to this chat" to the correct
    # action + target combo.
    # ------------------------------------------------------------------
    voice_msg = (
        "Send a voice note (in Spanish) to the group "
        "11235677890-2222222222@g.us making a silly joke "
        "(we need to test your capabilities)"
    )
    result = await agent.chat(
        voice_msg,
        output_cls=WahaAction,
        conversation_id="operator",
        context=_operator_ctx(),
        tools=[],
        extra_system_context=_OPERATOR_TURN_CONTEXT,
        reply_style=REPLY_STYLE,
    )
    ok = _print_result("send_voice_note turn", result)
    action = result.action
    if not ok:
        failures.append("send_voice_note: not a typed WahaAction or errored")
    elif action.action != "send_voice_note":
        failures.append(
            f"send_voice_note: expected action 'send_voice_note', got {action.action!r}"
        )
    elif not action.text:
        failures.append("send_voice_note: text is empty")
    elif not getattr(action, "target", None):
        failures.append("send_voice_note: target is empty (must point to the destination chat)")
    elif "2222222222" not in (getattr(action, "target", "") or ""):
        failures.append(
            f"send_voice_note: target mismatch, got {getattr(action, 'target', None)!r}"
        )

    # ------------------------------------------------------------------
    # Turn 3: tool-calling + structured output — web search + summarize.
    # The model must call web_search (and likely get_webpage_content) and
    # then resolve a console action with a grounded summary. This checks
    # that the tool loop and the terminal structured step compose.
    # ------------------------------------------------------------------
    web_tools = _select_tools(agent, ["web_search", "get_webpage_content"])
    result = await agent.chat(
        "Who won the 2026 FIFA World Cup? Search the web and summarize "
        "the result. Reply to me on the console.",
        output_cls=WahaAction,
        conversation_id="operator",
        context=_operator_ctx(),
        tools=web_tools,
        extra_system_context=_OPERATOR_TURN_CONTEXT,
        reply_style=REPLY_STYLE,
    )
    ok = _print_result("web_search + summarize turn", result)
    action = result.action
    if not ok:
        failures.append("web_search: not a typed WahaAction or errored")
    else:
        if not result.tool_calls:
            failures.append("web_search: no tool calls were made (model should have searched)")
        elif not any(tc.name == "web_search" for tc in result.tool_calls):
            failures.append(
                "web_search: web_search tool was not called, got: "
                + ", ".join(tc.name for tc in result.tool_calls)
            )
        if action.action != "console":
            failures.append(f"web_search: expected action 'console', got {action.action!r}")
        elif not action.text:
            failures.append("web_search: summary text is empty")

    # ------------------------------------------------------------------
    # Turn 4: operator turn requiring get_hardware_info tool call.
    # The model must inspect the host's hardware before replying with a
    # console action summarizing the specs.
    # ------------------------------------------------------------------
    hw_tools = _select_tools(agent, ["get_hardware_info"])
    result = await agent.chat(
        "What are the specs of this server? Check the hardware and tell me "
        "CPU, memory and disk usage. Reply to me on the console.",
        output_cls=WahaAction,
        conversation_id="operator",
        context=_operator_ctx(),
        tools=hw_tools,
        extra_system_context=_OPERATOR_TURN_CONTEXT,
        reply_style=REPLY_STYLE,
    )
    ok = _print_result("get_hardware_info turn", result)
    action = result.action
    if not ok:
        failures.append("hardware: not a typed WahaAction or errored")
    else:
        if not any(tc.name == "get_hardware_info" for tc in result.tool_calls):
            failures.append(
                "hardware: get_hardware_info tool was not called, got: "
                + ", ".join(tc.name for tc in result.tool_calls)
                or "(none)"
            )
        if action.action != "console":
            failures.append(f"hardware: expected action 'console', got {action.action!r}")
        elif not action.text:
            failures.append("hardware: summary text is empty")

    # ------------------------------------------------------------------
    # Turn 5: operator directive that mixes a tool call with a send action
    # — fetch the weather via tool, then send the result to a WhatsApp chat.
    # ------------------------------------------------------------------
    weather_tools = _select_tools(agent, ["get_weather"])
    result = await agent.chat(
        "Check the current weather in New York using the get_weather "
        "tool, then send a short summary of it to the WhatsApp group "
        "11235677890-1111111111@g.us.",
        output_cls=WahaAction,
        conversation_id="operator",
        context=_operator_ctx(),
        tools=weather_tools,
        extra_system_context=_OPERATOR_TURN_CONTEXT,
        reply_style=REPLY_STYLE,
    )
    ok = _print_result("get_weather + send_to_group turn", result)
    action = result.action
    if not ok:
        failures.append("weather+send: not a typed WahaAction or errored")
    else:
        if not any(tc.name == "get_weather" for tc in result.tool_calls):
            failures.append(
                "weather+send: get_weather tool was not called, got: "
                + ", ".join(tc.name for tc in result.tool_calls)
                or "(none)"
            )
        if action.action not in ("send_to_group", "send_dm"):
            failures.append(f"weather+send: expected send action, got {action.action!r}")
        elif not getattr(action, "target", None):
            failures.append("weather+send: target is empty")
        elif not action.text:
            failures.append("weather+send: text is empty")

    print("\n" + "=" * 60)
    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all structured-output + tool-calling turns resolved into typed WahaAction")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
