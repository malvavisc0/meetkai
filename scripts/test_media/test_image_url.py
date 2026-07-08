"""Test 1: Image from local file -> extract_media (URL simulation) -> vision pipeline.

Loads a local .webp image, simulates a WAHA URL-based payload, and passes
the bytes to the LLM via ImageBlock.

Usage:
    uv run python scripts/test_media/test_image_url.py
"""

import asyncio
import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from kai.agent.context import MessageContext
from kai.agent.core import ActionResult, KaiAgent
from kai.bots.waha.media import MediaType, extract_media
from kai.config.settings import get_settings


class _ScriptAction(ActionResult):
    action: Literal["reply", "silent"]
    text: str | None = None


SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_PATH = SCRIPT_DIR / "test_image.webp"


async def main():
    print("=" * 60)
    print("TEST: Image from local file -> Vision Pipeline")
    print("=" * 60)

    image_bytes = IMAGE_PATH.read_bytes()
    print(f"[OK] Loaded {IMAGE_PATH.name}: {len(image_bytes)} bytes")

    msg = {
        "type": "image",
        "mimetype": "image/webp",
        "filename": "test.webp",
        "mediaUrl": "http://localhost:3000/api/files/default/test.webp",
    }
    media = extract_media(msg)
    assert media is not None, "extract_media returned None"
    assert media.type is MediaType.IMAGE
    assert media.url is not None
    assert media.data is None
    print(f"[OK] extract_media -> type={media.type}, url-based")

    settings = get_settings()
    agent = KaiAgent(settings)
    context = MessageContext(sender_name="TestUser", sender_id="test@lid", conversation_id="test")

    reply = await agent.chat(
        "Describe what you see in this image in one sentence.",
        output_cls=_ScriptAction,
        conversation_id="test-image-url",
        context=context,
        images=[image_bytes],
    )
    reply = reply.reply
    assert reply, "Agent returned empty reply"
    assert reply.strip() != "<<silent>>", "Agent returned silent"
    print(f"[OK] Agent reply: {reply[:120]}")

    print("=" * 60)
    print("PASS: Image URL pipeline works end-to-end")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
