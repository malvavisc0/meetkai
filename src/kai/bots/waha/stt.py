from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class STTProvider(ABC):
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
        media_tmp = Path("/tmp/kai/media")
        media_tmp.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(dir=media_tmp))
        try:
            input_path = work_dir / "input"
            input_path.write_bytes(audio_bytes)

            wav_path = work_dir / "output.wav"
            self._convert_to_wav(input_path, wav_path)

            txt_path = self._run_whisper(wav_path, work_dir)

            if txt_path.exists():
                return txt_path.read_text(encoding="utf-8").strip()
            return ""
        except Exception:
            logger.exception("Whisper transcription failed")
            return ""
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _convert_to_wav(self, input_path: Path, output_path: Path) -> None:
        cmd = [
            self._ffmpeg,
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
    def __init__(
        self,
        ffmpeg_path: str,
        whisper_server_path: str,
        model_path: str,
        language: str = "auto",
        host: str = "127.0.0.1",
        port: int = 8787,
        threads: int = 4,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._server_bin = whisper_server_path
        self._model = model_path
        self._language = language
        self._host = host
        self._port = port
        self._threads = threads
        self._process: subprocess.Popen | None = None
        self._base_url = f"http://{host}:{port}"

    async def start(self) -> None:
        if self._process and self._process.poll() is None:
            return

        logger.info(
            "Starting whisper-server on %s:%d (model=%s, lang=%s)",
            self._host,
            self._port,
            self._model,
            self._language,
        )
        self._process = subprocess.Popen(
            [
                self._server_bin,
                "-m",
                self._model,
                "-l",
                self._language,
                "-t",
                str(self._threads),
                "--host",
                self._host,
                "--port",
                str(self._port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        for attempt in range(60):
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode() if self._process.stderr else ""
                raise RuntimeError(f"whisper-server exited during startup: {stderr}")
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{self._base_url}/health", timeout=1)
                    if resp.status_code == 200:
                        logger.info("whisper-server ready (pid=%d)", self._process.pid)
                        return
            except (httpx.ConnectError, httpx.ReadTimeout):
                pass
            await asyncio.sleep(1)

        raise TimeoutError("whisper-server did not become ready within 60s")

    async def stop(self) -> None:
        if self._process and self._process.poll() is None:
            logger.info("Stopping whisper-server (pid=%d)", self._process.pid)
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            logger.info("whisper-server stopped")
        self._process = None

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "") -> str:
        media_tmp = Path("/tmp/kai/media")
        media_tmp.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(dir=media_tmp))
        try:
            input_path = work_dir / "input"
            input_path.write_bytes(audio_bytes)

            wav_path = work_dir / "output.wav"
            self._convert_to_wav(input_path, wav_path)

            return await self._inference(wav_path)
        except Exception:
            logger.exception("Whisper server transcription failed")
            return ""
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _convert_to_wav(self, input_path: Path, output_path: Path) -> None:
        cmd = [
            self._ffmpeg,
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
    server_threads: int = 4,
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

    model = Path(model_path)
    if not model.is_file():
        logger.warning("whisper model not found at %s — voice transcription disabled", model_path)
        return NoopSTT()

    if server_mode:
        whisper_server = Path(whisper_cpp_path).parent / "whisper-server"
        if not whisper_server.is_file():
            logger.warning(
                "whisper-server not found at %s — falling back to CLI mode", whisper_server
            )
        else:
            return WhisperServerSTT(
                ffmpeg_path=ffmpeg_path,
                whisper_server_path=str(whisper_server),
                model_path=model_path,
                language=language,
                host=server_host,
                port=server_port,
                threads=server_threads,
            )

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
