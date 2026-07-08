"""Video compression + audio extraction for the vision channel.

WhatsApp videos are multi-MB and 30fps — far more than a vision model needs.
This re-encodes with libx264: scales to <=480px wide, drops to 10fps, drops
audio from the mp4 (vision models don't use it), CRF 30, yuv420p, +faststart.
In the same pass it extracts the audio track to a 16k-mono WAV for local
whisper transcription (gemma-4 has no audio modality, so a talking-selfie
video gets its words transcribed as a text tag instead of being silent).

The compressed mp4 is small enough to inline as a base64 data URL in the LLM
request without blowing up context or cost. The WAV is fed to the existing
``stt`` pipeline unchanged.

ffmpeg is synchronous, so callers wrap this in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 60
_MAX_WIDTH = 480
_FPS = 10
_CRF = 30


def resolve_ffmpeg(ffmpeg_path: str) -> str | None:
    """Vendor -> system resolution, same pattern as the inline ffmpeg lookup
    in stt.create_stt_provider (there is no standalone ``_resolve_whisper``
    function; the vendor/system fallback logic lives inline there)."""
    ffmpeg = Path(ffmpeg_path)
    if ffmpeg.is_file():
        return str(ffmpeg)
    system = shutil.which("ffmpeg")
    if system:
        logger.info("Using system ffmpeg at %s", system)
        return system
    logger.warning("ffmpeg not found - video compression disabled")
    return None


def compress_video(src_bytes: bytes, ffmpeg_path: str) -> tuple[bytes | None, bytes | None]:
    """Re-encode *src_bytes* to a small silent mp4 AND extract a 16k-mono WAV.

    Returns ``(compressed_mp4, audio_wav)``. Either element may be ``None``
    if that output failed (e.g. a silent video has no audio track); the caller
    falls back to the WAHA JPEG thumbnail for the vision channel and skips
    transcription when ``audio_wav`` is ``None``. Both ``None`` means the whole
    pass failed — caller falls back to thumbnail + caption only.
    """
    src = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    dst_mp4 = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    dst_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        src.write(src_bytes)
        src.close()
        dst_mp4.close()
        dst_wav.close()

        mp4_cmd = [
            ffmpeg_path,
            "-y",
            "-loglevel",
            "error",
            "-i",
            src.name,
            "-an",
            "-vf",
            f"scale='min({_MAX_WIDTH},iw)':-2,fps={_FPS}",
            "-c:v",
            "libx264",
            "-crf",
            str(_CRF),
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            dst_mp4.name,
        ]
        r = subprocess.run(mp4_cmd, capture_output=True, timeout=_TIMEOUT_SECONDS)
        if r.returncode != 0:
            logger.warning(
                "ffmpeg video encode failed (rc=%d): %s",
                r.returncode,
                r.stderr.decode(errors="replace")[:300],
            )
            return None, None

        wav_cmd = [
            ffmpeg_path,
            "-y",
            "-loglevel",
            "error",
            "-i",
            src.name,
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            dst_wav.name,
        ]
        wav_result = subprocess.run(wav_cmd, capture_output=True, timeout=_TIMEOUT_SECONDS)
        if wav_result.returncode != 0:
            logger.info(
                "ffmpeg audio extraction skipped (no audio track?): %s",
                wav_result.stderr.decode(errors="replace")[:200],
            )

        mp4_bytes = None
        if os.path.getsize(dst_mp4.name) > 0:
            with open(dst_mp4.name, "rb") as f:
                mp4_bytes = f.read()
        wav_bytes = None
        if os.path.getsize(dst_wav.name) > 0:
            with open(dst_wav.name, "rb") as f:
                wav_bytes = f.read()
        return mp4_bytes, wav_bytes
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timed out after %ds", _TIMEOUT_SECONDS)
        return None, None
    except Exception as exc:
        logger.warning("Video compression failed: %s", exc)
        return None, None
    finally:
        for p in (src.name, dst_mp4.name, dst_wav.name):
            try:
                os.unlink(p)
            except OSError:
                pass
