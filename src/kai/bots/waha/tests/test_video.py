"""Tests for the ffmpeg video compression + audio extraction pass.

These exercise ``kai.bots.waha.video.compress_video`` end to end against a
real ffmpeg: the function shells out to ffmpeg, so the only faithful test is
to run it. They are skipped when ffmpeg is unavailable (CI without the
binary), since ``compress_video`` would return ``(None, None)``.
"""

import shutil
import subprocess
import wave
from io import BytesIO

import pytest

from kai.bots.waha.video import compress_video, resolve_ffmpeg


def _ffmpeg() -> str | None:
    """Return a usable ffmpeg binary, or None."""
    return resolve_ffmpeg("vendor/ffmpeg/ffmpeg") or shutil.which("ffmpeg")


def _make_source_mp4(ffmpeg: str, tmp_path) -> bytes:
    """Generate a small source clip WITH an audio track for compression tests."""
    src = tmp_path / "src.mp4"
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        # Visual: a test pattern with motion so the encoder has real work.
        "-f",
        "lavfi",
        "-i",
        "testsrc2=duration=2:size=640x480:rate=25",
        # Audio: a sine tone so the WAV extraction has something to extract.
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(src),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return src.read_bytes()


pytestmark = pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg not available")


def test_compress_video_returns_smaller_mp4_and_16k_mono_wav(tmp_path):
    ffmpeg = _ffmpeg()
    assert ffmpeg is not None
    src = _make_source_mp4(ffmpeg, tmp_path)

    mp4, wav = compress_video(src, ffmpeg)

    assert mp4 is not None, "compressed mp4 must be produced when ffmpeg succeeds"
    assert wav is not None, "audio wav must be extracted from a clip with sound"
    # Valid mp4: bytes 4..8 of an MP4 file are the 'ftyp' box brand.
    assert len(mp4) >= 8 and mp4[4:8] == b"ftyp", "compressed output is not a valid mp4"
    # The whole point of compression: the vision payload shrinks.
    assert len(mp4) < len(src), (
        f"compressed mp4 ({len(mp4)} B) should be smaller than source ({len(src)} B)"
    )

    # The WAV must be exactly the format whisper expects: 16 kHz, mono, 16-bit PCM.
    with wave.open(BytesIO(wav), "rb") as w:
        assert w.getframerate() == 16000, f"wav framerate {w.getframerate()} != 16000"
        assert w.getnchannels() == 1, f"wav channels {w.getnchannels()} != 1"
        assert w.getsampwidth() == 2, f"wav sample width {w.getsampwidth()} != 2 bytes"


def test_compress_video_handles_missing_binary(tmp_path):
    # A nonexistent ffmpeg path must not raise; it returns (None, None).
    mp4, wav = compress_video(b"not a real video", "/nonexistent/ffmpeg")
    assert mp4 is None
    assert wav is None
