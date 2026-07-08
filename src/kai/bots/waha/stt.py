from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


# Map common language names (as given via --language) to whisper.cpp language
# codes. Keys are matched case-insensitively. "auto" / unknown names fall
# through to whisper's own auto-detection.
_LANGUAGE_NAME_TO_CODE: dict[str, str] = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "arabic": "ar",
    "hindi": "hi",
    "turkish": "tr",
    "polish": "pl",
    "ukrainian": "uk",
    "swedish": "sv",
    "norwegian": "no",
    "finnish": "fi",
    "danish": "da",
    "czech": "cs",
    "greek": "el",
    "hebrew": "he",
    "romanian": "ro",
    "hungarian": "hu",
    "vietnamese": "vi",
    "thai": "th",
    "indonesian": "id",
    "catalan": "ca",
    "galician": "gl",
    "basque": "eu",
}


def resolve_whisper_language(language: str) -> str:
    """Resolve a language name (e.g. "Spanish") to a whisper code (e.g. "es").

    Returns "auto" for empty/auto/unknown input so whisper auto-detects.
    """
    if not language:
        return "auto"
    lang = language.strip().lower()
    if lang in ("auto", ""):
        return "auto"
    return _LANGUAGE_NAME_TO_CODE.get(lang, "auto")


class STTProvider(ABC):
    healthy: bool = True

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, mime_type: str = "") -> str: ...

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class NoopSTT(STTProvider):
    async def transcribe(self, audio_bytes: bytes, mime_type: str = "") -> str:
        logger.warning("STT unavailable (whisper.cpp binary or model missing)")
        return ""


def _convert_to_wav(ffmpeg_path: str, input_path: Path, output_path: Path) -> None:
    """Re-encode arbitrary audio to 16kHz mono WAV — the format whisper expects.

    Shared by the CLI and server STT providers (previously duplicated in
    both). Raises ``RuntimeError`` with ffmpeg's stderr on failure.
    """
    cmd = [
        ffmpeg_path,
        "-i",
        str(input_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        "-y",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode}): {result.stderr.decode()}")


@contextmanager
def _stt_work_dir() -> Generator[Path]:
    """Yield a temporary directory for STT processing, cleaning up on exit.

    Shared by ``WhisperCppSTT`` and ``WhisperServerSTT`` so the
    create-tempdir / write-input / convert / cleanup lifecycle isn't
    duplicated.
    """
    media_tmp = Path("/tmp/kai/media")
    media_tmp.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(dir=media_tmp))
    try:
        yield work_dir
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


class WhisperCppSTT(STTProvider):
    def __init__(
        self,
        ffmpeg_path: str,
        whisper_cpp_path: str,
        model_path: str,
        language: str = "auto",
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._whisper = whisper_cpp_path
        self._model = model_path
        self._language = language

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "") -> str:
        with _stt_work_dir() as work_dir:
            try:
                input_path = work_dir / "input"
                input_path.write_bytes(audio_bytes)

                wav_path = work_dir / "output.wav"
                _convert_to_wav(self._ffmpeg, input_path, wav_path)

                txt_path = self._run_whisper(wav_path, work_dir)

                if txt_path.exists():
                    return txt_path.read_text(encoding="utf-8").strip()
                return ""
            except Exception:
                logger.exception("Whisper transcription failed")
                return ""

    def _run_whisper(self, wav_path: Path, work_dir: Path) -> Path:
        prefix = str(work_dir / "whisper_out")
        cmd = [
            self._whisper,
            "-m",
            self._model,
            "-f",
            str(wav_path),
            "-l",
            self._language,
            "--output-txt",
            "-of",
            prefix,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(
                f"whisper.cpp failed (rc={result.returncode}): {result.stderr.decode()}"
            )
        return Path(f"{prefix}.txt")


class WhisperServerSTT(STTProvider):
    """Pure HTTP client of an already-running whisper-server.

    The cockpit owns the server process (``MediaServiceManager``); this class
    only probes ``/health`` and POSTs to ``/inference``.
    """

    def __init__(
        self,
        ffmpeg_path: str,
        host: str = "127.0.0.1",
        port: int = 8787,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._host = host
        self._port = port
        self._base_url = f"http://{host}:{port}"
        self.healthy: bool = False

    async def start(self) -> None:
        """Probe the server's /health endpoint.

        Does NOT spawn a process — the cockpit owns the server.  Sets
        ``self.healthy`` so the caller can swap in ``NoopSTT`` when the
        server is unreachable.
        """
        for _attempt in range(5):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{self._base_url}/health", timeout=2)
                    if resp.status_code == 200:
                        self.healthy = True
                        logger.info("whisper-server healthy at %s", self._base_url)
                        return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPError):
                pass
            await asyncio.sleep(0.5)
        logger.warning("whisper-server unhealthy at %s", self._base_url)
        self.healthy = False

    async def stop(self) -> None:
        """No-op — the cockpit owns the server process."""

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "") -> str:
        with _stt_work_dir() as work_dir:
            try:
                input_path = work_dir / "input"
                input_path.write_bytes(audio_bytes)

                wav_path = work_dir / "output.wav"
                _convert_to_wav(self._ffmpeg, input_path, wav_path)

                return await self._inference(wav_path)
            except Exception:
                logger.exception("Whisper server transcription failed")
                return ""

    async def _inference(self, wav_path: Path) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            with open(wav_path, "rb") as f:
                resp = await client.post(
                    f"{self._base_url}/inference",
                    files={"file": (wav_path.name, f, "audio/wav")},
                    data={"temperature": "0"},
                )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("text", "")
            return " ".join(text.split()).strip()


def create_stt_provider(
    ffmpeg_path: str,
    whisper_cpp_path: str,
    model_path: str,
    language: str = "auto",
    *,
    server_mode: bool = False,
    server_host: str = "127.0.0.1",
    server_port: int = 8787,
) -> STTProvider:
    ffmpeg = Path(ffmpeg_path)
    if not ffmpeg.is_file():
        vendor_ffmpeg = Path(whisper_cpp_path).parent.parent / "ffmpeg" / "ffmpeg"
        if vendor_ffmpeg.is_file():
            ffmpeg_path = str(vendor_ffmpeg)
            logger.info("Using vendor ffmpeg at %s", ffmpeg_path)
        elif system_ffmpeg := shutil.which("ffmpeg"):
            ffmpeg_path = system_ffmpeg
            logger.info("Using system ffmpeg at %s", ffmpeg_path)
        else:
            logger.warning("ffmpeg not found — voice transcription disabled")
            return NoopSTT()

    if server_mode:
        return WhisperServerSTT(
            ffmpeg_path=ffmpeg_path,
            host=server_host,
            port=server_port,
        )

    model = Path(model_path)
    if not model.is_file():
        logger.warning("whisper model not found at %s — voice transcription disabled", model_path)
        return NoopSTT()

    whisper = Path(whisper_cpp_path)
    if not whisper.is_file():
        logger.warning(
            "whisper.cpp not found at %s — voice transcription disabled", whisper_cpp_path
        )
        return NoopSTT()

    return WhisperCppSTT(
        ffmpeg_path=ffmpeg_path,
        whisper_cpp_path=whisper_cpp_path,
        model_path=model_path,
        language=language,
    )
