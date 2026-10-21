import base64
import json

from llama_index.core.base.llms.types import TextBlock, VideoBlock
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.llms.openai import utils as openai_utils

from kai.agent.core import KaiAgent
from kai.agent.goal import GoalManager
from tests.agent_test_helpers import _TestAction


class TestLiteralNewlineUnescape:
    """Models sometimes serialize a multi-line reply as a single line with
    literal backslash-n tokens (double-escaped JSON value, or a ``key: value``
    line). The terminal normalization step must turn those into real line
    breaks so the delivered message isn't full of visible ``\\n`` garbage.
    """

    def test_unescapes_literal_newlines_when_no_real_newlines(self):
        from kai.agent.core import unescape_literal_newlines

        body = "Hello there,\\n\\nhere is the comparison:\\n\\n### 1. GPU"
        out = unescape_literal_newlines(body)
        assert out == "Hello there,\n\nhere is the comparison:\n\n### 1. GPU"

    def test_leaves_real_newlines_untouched(self):
        from kai.agent.core import unescape_literal_newlines

        body = "line one\nline two\\nstill line two?"
        # Already has a real newline -> nothing is mangled, literal \\n kept.
        assert unescape_literal_newlines(body) == body

    def test_does_not_mangle_single_literal_backslash_n(self):
        from kai.agent.core import unescape_literal_newlines

        # A Windows-style path has one literal \\n; not the multi-line
        # signature, so it must be preserved verbatim.
        assert unescape_literal_newlines("C:\\new folder") == "C:\\new folder"

    def test_parse_structured_text_unescapes_double_escaped_json(self):
        # The model returned valid JSON but double-escaped the newlines in the
        # body (``\\n`` in the JSON source -> literal backslash-n after parse).
        # The terminal step must recover real line breaks so the email body
        # doesn't render as a wall of ``\\n`` tokens.
        from llama_index.core.output_parsers.pydantic import PydanticOutputParser

        from kai.agent.core import KaiAgent

        parser = PydanticOutputParser(output_cls=_TestAction)
        # json.dumps escapes the real newlines once; the literal doubling here
        # mimics the model emitting ``\\n`` in the JSON string value.
        payload = json.dumps(
            {
                "action": "reply",
                "text": "Hello there,\\n\\nhere is the plan:\\n\\n1. step",
            }
        )

        action = KaiAgent.parse_structured_text(payload, _TestAction, parser)

        assert action.action == "reply"
        assert action.text == "Hello there,\n\nhere is the plan:\n\n1. step"


class TestVideoBlockShim:
    """The import-time monkeypatch in ``kai.agent.openai_compat`` (imported by
    ``kai.agent.core`` for its side effect) makes llama_index's
    OpenAI adapter serialize ``VideoBlock`` as the verified ``video_url``
    Chat Completions shape. Without the shim
    the adapter raises ``ValueError("Unsupported content block type")``.
    """

    def test_video_block_emits_video_url_data_uri(self):
        mp4 = b"\x00\x00\x00\x1cftypmp42" + b"\x00" * 64
        message = ChatMessage(
            role=MessageRole.USER,
            blocks=[
                TextBlock(text="describe this"),
                VideoBlock(video=mp4, video_mimetype="video/mp4"),
            ],
        )
        out = openai_utils.to_openai_message_dict(message)
        assert out["role"] == "user"
        content = out["content"]
        assert isinstance(content, list)
        types = [block["type"] for block in content]
        assert types == ["text", "video_url"]
        video_block = content[1]
        expected_url = f"data:video/mp4;base64,{base64.b64encode(mp4).decode()}"
        assert video_block == {"type": "video_url", "video_url": {"url": expected_url}}

    def test_text_only_message_delegates_to_original(self):
        # A message with no VideoBlock must go through the original converter
        # unchanged (string content, not a content-block list).
        message = ChatMessage(role=MessageRole.USER, content="just text")
        out = openai_utils.to_openai_message_dict(message)
        content = out.get("content")
        assert content == "just text"

    def test_video_block_with_url_uses_url_directly(self):
        message = ChatMessage(
            role=MessageRole.USER,
            blocks=[VideoBlock(url="https://example.com/clip.mp4", video_mimetype="video/mp4")],
        )
        out = openai_utils.to_openai_message_dict(message)
        content = out.get("content")
        assert isinstance(content, list)
        assert content[0] == {
            "type": "video_url",
            "video_url": {"url": "https://example.com/clip.mp4"},
        }

    def test_build_messages_attaches_video_block(self):
        agent = KaiAgent(settings=None, goal_manager=GoalManager())
        mp4 = b"\x00\x00\x00\x1cftypmp42"
        messages = agent.build_messages("hi", videos=[mp4])
        user_msg = messages[-1]
        assert user_msg.role == MessageRole.USER
        block_types = [type(b).__name__ for b in user_msg.blocks]
        assert block_types == ["TextBlock", "VideoBlock"]
