"""Test 3: Voice note -> ffmpeg -> whisper.cpp -> transcription.

Loads a local .oga voice note, converts to WAV with ffmpeg, transcribes
with whisper.cpp (Spanish ggml-small model), and feeds to the agent.

Usage (from project root):
    uv run python scripts/test_media/test_voice.py           # CLI mode
    uv run python scripts/test_media/test_voice.py --server   # server mode
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from kai.agent.context import MessageContext
from kai.agent.core import ActionResult, KaiAgent
from kai.bots.waha.media import MediaType, extract_media
from kai.bots.waha.stt import WhisperCppSTT, WhisperServerSTT, create_stt_provider
from kai.config.settings import get_settings


class _ScriptAction(ActionResult):
    action: Literal["reply", "silent"]
    text: str | None = None


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
VOICE_PATH = SCRIPT_DIR / "test_voice.oga"

FFMPEG = "/usr/bin/ffmpeg"
WHISPER_CPP = str(PROJECT_ROOT / "vendor" / "whisper.cpp" / "whisper-cli")
MODEL = str(PROJECT_ROOT / "models" / "whisper" / "ggml-small.bin")


async def main(server_mode: bool):
    mode_label = "SERVER" if server_mode else "CLI"
    print("=" * 60)
    print(f"TEST: Voice Note -> STT Pipeline (Spanish, {mode_label})")
    print("=" * 60)

    voice_bytes = VOICE_PATH.read_bytes()
    print(f"[OK] Loaded {VOICE_PATH.name}: {len(voice_bytes)} bytes")

    msg = {
        "type": "ptt",
        "mimetype": "audio/ogg; codecs=opus",
        "filename": "voice.oga",
        "mediaUrl": "http://localhost:3000/api/files/default/voice.oga",
    }
    media = extract_media(msg)
    assert media is not None, "extract_media returned None"
    assert media.type is MediaType.VOICE
    print(f"[OK] extract_media -> type={media.type}, mime={media.mime_type}")

    stt = create_stt_provider(
        FFMPEG,
        WHISPER_CPP,
        MODEL,
        language="es",
        server_mode=server_mode,
    )
    expected_type = WhisperServerSTT if server_mode else WhisperCppSTT
    assert isinstance(stt, expected_type), (
        f"Expected {expected_type.__name__}, got {type(stt).__name__}"
    )
    print(f"[OK] create_stt_provider -> {type(stt).__name__}")

    if server_mode:
        print("[..] Probing whisper-server /health ...")
        await stt.start()
        if not stt.healthy:
            print("[FAIL] whisper-server not healthy; is it running?")
            sys.exit(1)
        print("[OK] whisper-server healthy")

    transcription = await stt.transcribe(voice_bytes, mime_type=media.mime_type)
    assert transcription, "Transcription is empty"
    print(f"[OK] Transcription: {transcription}")

    if server_mode:
        transcription2 = await stt.transcribe(voice_bytes, mime_type=media.mime_type)
        assert transcription2, "Second transcription is empty"
        print(f"[OK] Second call (no reload): {transcription2}")
        await stt.stop()

    settings = get_settings()
    agent = KaiAgent(settings)
    context = MessageContext(sender_name="TestUser", sender_id="test@lid", conversation_id="test")

    voice_text = f"[voice note: {transcription}]"
    reply = await agent.chat(
        voice_text,
        output_cls=_ScriptAction,
        conversation_id="test-voice",
        context=context,
    )
    reply = reply.reply
    assert reply, "Agent returned empty reply"
    assert reply.strip() != "<<silent>>", "Agent returned silent"
    print(f"[OK] Agent reply: {reply[:120]}")

    print("=" * 60)
    print(f"PASS: Voice note pipeline works end-to-end ({mode_label})")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true", help="Use whisper-server mode")
    args = parser.parse_args()
    asyncio.run(main(args.server))
