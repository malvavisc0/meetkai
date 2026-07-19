from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai.bots.waha.stt import (
    NoopSTT,
    WhisperCppSTT,
    WhisperServerSTT,
    create_stt_provider,
)


class TestNoopSTT:
    @pytest.mark.asyncio
    async def test_returns_empty_string(self):
        stt = NoopSTT()
        result = await stt.transcribe(b"audio-bytes")
        assert result == ""


class TestWhisperCppSTT:
    @pytest.mark.asyncio
    @patch("kai.bots.waha.stt.subprocess.run")
    async def test_transcribe_calls_ffmpeg_then_whisper(self, mock_run, tmp_path):
        whisper_out = tmp_path / "whisper_out.txt"
        whisper_out.write_text("hello world")

        def side_effect(cmd, **kwargs):
            if "ffmpeg" in cmd[0]:
                wav_path = Path(cmd[-1])
                wav_path.write_bytes(b"RIFF....WAVE")
                return MagicMock(returncode=0, stderr=b"")
            if "whisper" in cmd[0] or "main" in cmd[0]:
                prefix = cmd[cmd.index("-of") + 1]
                Path(f"{prefix}.txt").write_text("hello world")
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=1, stderr=b"unknown command")

        mock_run.side_effect = side_effect

        stt = WhisperCppSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            whisper_cpp_path="/usr/bin/whisper",
            model_path="/models/ggml-base.bin",
            language="auto",
        )
        result = await stt.transcribe(b"fake-audio-bytes", mime_type="audio/ogg")
        assert result == "hello world"
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    @patch("kai.bots.waha.stt.subprocess.run")
    async def test_ffmpeg_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr=b"ffmpeg error")

        stt = WhisperCppSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            whisper_cpp_path="/usr/bin/whisper",
            model_path="/models/ggml-base.bin",
        )
        result = await stt.transcribe(b"bad-audio")
        assert result == ""

    @pytest.mark.asyncio
    @patch("kai.bots.waha.stt.subprocess.run")
    async def test_whisper_failure_returns_empty(self, mock_run):
        def side_effect(cmd, **kwargs):
            if "ffmpeg" in cmd[0]:
                wav_path = Path(cmd[-1])
                wav_path.write_bytes(b"RIFF....WAVE")
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=1, stderr=b"whisper error")

        mock_run.side_effect = side_effect

        stt = WhisperCppSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            whisper_cpp_path="/usr/bin/whisper",
            model_path="/models/ggml-base.bin",
        )
        result = await stt.transcribe(b"audio-bytes")
        assert result == ""

    @pytest.mark.asyncio
    @patch("kai.bots.waha.stt.subprocess.run")
    @patch("kai.bots.waha.stt._stt_work_dir")
    async def test_temp_dir_cleaned_up(self, mock_work_dir, mock_run, tmp_path):
        """Confirm the STT work dir is removed after transcribe() — happy path."""

        def side_effect(cmd, **kwargs):
            if "ffmpeg" in cmd[0]:
                wav_path = Path(cmd[-1])
                wav_path.write_bytes(b"RIFF....WAVE")
                return MagicMock(returncode=0, stderr=b"")
            if "whisper" in cmd[0] or "main" in cmd[0]:
                prefix = cmd[cmd.index("-of") + 1]
                Path(f"{prefix}.txt").write_text("hello world")
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=1, stderr=b"unknown command")

        mock_run.side_effect = side_effect

        work_dir = tmp_path / "stt_work"
        work_dir.mkdir()

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=work_dir)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_work_dir.return_value = ctx

        stt = WhisperCppSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            whisper_cpp_path="/usr/bin/whisper",
            model_path="/models/ggml-base.bin",
        )
        await stt.transcribe(b"fake-audio-bytes")

        assert (work_dir / "input").exists()
        assert (work_dir / "output.wav").exists()
        assert (work_dir / "whisper_out.txt").exists()
        assert mock_work_dir.called
        mock_work_dir.return_value.__exit__.assert_called_once()


class TestWhisperServerSTT:
    @pytest.mark.asyncio
    @patch("kai.bots.waha.stt.subprocess.run")
    @patch("kai.bots.waha.stt.httpx.AsyncClient")
    async def test_transcribe_via_server(self, mock_client_cls, mock_run):
        def run_side_effect(cmd, **kwargs):
            if "ffmpeg" in cmd[0]:
                wav_path = Path(cmd[-1])
                wav_path.write_bytes(b"RIFF....WAVE")
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=0, stderr=b"")

        mock_run.side_effect = run_side_effect

        mock_health_resp = MagicMock(status_code=200)
        mock_infer_resp = MagicMock()
        mock_infer_resp.status_code = 200
        mock_infer_resp.raise_for_status = MagicMock()
        mock_infer_resp.json.return_value = {"text": "  hello world  "}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_health_resp)
        mock_client.post = AsyncMock(return_value=mock_infer_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        stt = WhisperServerSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            host="127.0.0.1",
            port=8787,
        )

        await stt.start()
        assert stt.healthy is True
        result = await stt.transcribe(b"fake-audio", mime_type="audio/ogg")
        await stt.stop()

        assert result == "hello world"

    @pytest.mark.asyncio
    @patch("kai.bots.waha.stt.httpx.AsyncClient")
    async def test_start_sets_unhealthy_on_connect_error(self, mock_client_cls):
        import httpx as real_httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=real_httpx.ConnectError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        stt = WhisperServerSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            host="127.0.0.1",
            port=8787,
        )

        await stt.start()
        assert stt.healthy is False

    @pytest.mark.asyncio
    async def test_stop_noop(self):
        stt = WhisperServerSTT(
            ffmpeg_path="/usr/bin/ffmpeg",
            host="127.0.0.1",
            port=8787,
        )
        await stt.stop()
        assert stt.healthy is False


class TestCreateSTTProvider:
    def test_returns_whisper_when_binaries_exist(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"fake")
        whisper = tmp_path / "main"
        whisper.write_bytes(b"fake")
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake")

        provider = create_stt_provider(str(ffmpeg), str(whisper), str(model))
        assert isinstance(provider, WhisperCppSTT)

    @patch("kai.bots.waha.stt.shutil.which", return_value=None)
    def test_returns_noop_when_ffmpeg_missing(self, _mock_which, tmp_path):
        whisper = tmp_path / "main"
        whisper.write_bytes(b"fake")
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake")

        provider = create_stt_provider("/nonexistent/ffmpeg", str(whisper), str(model))
        assert isinstance(provider, NoopSTT)

    def test_returns_noop_when_whisper_missing(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"fake")
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake")

        provider = create_stt_provider(str(ffmpeg), "/nonexistent/whisper", str(model))
        assert isinstance(provider, NoopSTT)

    def test_returns_noop_when_model_missing(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"fake")
        whisper = tmp_path / "main"
        whisper.write_bytes(b"fake")

        provider = create_stt_provider(str(ffmpeg), str(whisper), "/nonexistent/model.bin")
        assert isinstance(provider, NoopSTT)

    @patch("kai.bots.waha.stt.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_falls_back_to_system_ffmpeg(self, _mock_which, tmp_path):
        whisper = tmp_path / "whisper-cli"
        whisper.write_bytes(b"fake")
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake")

        provider = create_stt_provider("/nonexistent/ffmpeg", str(whisper), str(model))
        assert isinstance(provider, WhisperCppSTT)

    def test_returns_server_client_when_server_mode(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"fake")
        whisper = tmp_path / "whisper-cli"
        whisper.write_bytes(b"fake")
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake")

        provider = create_stt_provider(str(ffmpeg), str(whisper), str(model), server_mode=True)
        assert isinstance(provider, WhisperServerSTT)

    def test_server_mode_does_not_require_binary(self, tmp_path):
        """server_mode constructs a pure client — no whisper-server binary needed."""
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"fake")
        model = tmp_path / "ggml-base.bin"
        model.write_bytes(b"fake")

        provider = create_stt_provider(
            str(ffmpeg), "/nonexistent/whisper-cli", str(model), server_mode=True
        )
        assert isinstance(provider, WhisperServerSTT)
