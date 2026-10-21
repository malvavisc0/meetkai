from typing import Any

from llama_index.core.base.llms.types import ContentBlock, TextBlock, VideoBlock
from llama_index.core.llms import ChatMessage
from llama_index.llms.openai import utils as openai_utils
from openai.types.chat import (
    ChatCompletionContentPartParam,
    ChatCompletionContentPartRefusalParam,
    ChatCompletionMessageParam,
)

# --- VideoBlock shim --------------------------------------------------------
# llama_index's OpenAI adapter does not serialize VideoBlock — it raises
# ValueError("Unsupported content block type").  This monkeypatch makes
# to_openai_message_dict emit the verified ``video_url`` shape for VideoBlock,
# delegating all other block types to the original implementation.

original_to_openai_message_dict = openai_utils.to_openai_message_dict

ContentPart = (
    dict[str, Any] | ChatCompletionContentPartParam | ChatCompletionContentPartRefusalParam
)


def to_openai_message_dict(
    message: ChatMessage,
    drop_none: bool = False,
    model: str | None = None,
    store: bool = False,
) -> dict[str, Any] | ChatCompletionMessageParam:
    """Serialize a chat message, adding OpenAI video block support."""
    if not any(isinstance(b, VideoBlock) for b in message.blocks):
        return original_to_openai_message_dict(
            message, drop_none=drop_none, model=model, store=store
        )
    content: list[ContentPart] = []
    content_txt = ""
    for block in message.blocks:
        txt = append_block_to_content(block, message, content, drop_none, model, store)
        if txt:
            content_txt += txt
    return {
        "role": message.role.value,
        "content": (
            content_txt if all(isinstance(b, TextBlock) for b in message.blocks) else content
        ),
    }


def append_block_to_content(
    block: ContentBlock,
    message: ChatMessage,
    content: list[ContentPart],
    drop_none: bool,
    model: str | None,
    store: bool,
) -> str:
    """Append one block's OpenAI representation to ``content``; return text added."""
    if isinstance(block, TextBlock):
        content.append({"type": "text", "text": block.text})
        return block.text
    if isinstance(block, VideoBlock):
        content.append({"type": "video_url", "video_url": {"url": video_url(block)}})
        return ""
    single = original_to_openai_message_dict(
        type(message)(
            role=message.role,
            blocks=[block],
            additional_kwargs=message.additional_kwargs,
        ),
        drop_none=drop_none,
        model=model,
        store=store,
    )
    item = single.get("content", "") if isinstance(single, dict) else single
    if isinstance(item, list):
        content.extend(item)
        return ""
    if isinstance(item, str):
        content.append({"type": "text", "text": item})
        return item
    return ""


def video_url(block: VideoBlock) -> str:
    """Return a video block's URL, inlining local data as base64."""
    if block.url:
        return str(block.url)
    vid = block.resolve_video(as_base64=True).read()
    mt = block.video_mimetype or "video/mp4"
    return f"data:{mt};base64,{vid.decode() if isinstance(vid, bytes) else vid}"


openai_utils.to_openai_message_dict = to_openai_message_dict
