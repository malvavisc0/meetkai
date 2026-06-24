"""Test 2: Image as inline base64 -> extraction -> vision pipeline.

Loads a local .webp image, encodes it as base64 (simulating an inline
WAHA payload), then runs it through extract_media and the agent.

Usage:
    uv run python scripts/test_media/test_image_base64.py
"""

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from kai.agent.context import MessageContext
from kai.agent.core import KaiAgent
from kai.bots.waha.media import MediaType, extract_media
from kai.config.settings import get_settings

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_PATH = SCRIPT_DIR / "test_image.webp"


async def main():
    print("=" * 60)
    print("TEST: Image as inline base64 -> Vision Pipeline")
    print("=" * 60)

    image_bytes = IMAGE_PATH.read_bytes()
    b64_str = base64.b64encode(image_bytes).decode()
    print(
        f"[OK] Loaded + encoded {IMAGE_PATH.name}: "
        f"{len(image_bytes)} bytes -> {len(b64_str)} b64 chars"
    )

    msg = {
        "type": "image",
        "mimetype": "image/webp",
        "filename": "test.webp",
        "data": b64_str,
    }
    media = extract_media(msg)
    assert media is not None, "extract_media returned None"
    assert media.type is MediaType.IMAGE
    assert media.data is not None
    assert media.url is None
    assert media.data == image_bytes
    print(f"[OK] extract_media -> decoded {len(media.data)} bytes from base64")

    settings = get_settings()
    agent = KaiAgent(settings)
    context = MessageContext(sender_name="TestUser", sender_id="test@lid", chat_id="test")

    reply = await agent.chat(
        "What do you see in this image? Answer briefly.",
        conversation_id="test-image-b64",
        context=context,
        images=[media.data],
    )
    assert reply, "Agent returned empty reply"
    assert reply.strip() != "<<silent>>", "Agent returned silent"
    print(f"[OK] Agent reply: {reply[:120]}")

    print("=" * 60)
    print("PASS: Image base64 pipeline works end-to-end")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
