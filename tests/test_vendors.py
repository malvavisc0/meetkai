"""Tests for the vendors registry + per-vendor install logic.

These use a temp project root and monkeypatch the network/build surfaces
(httpx download, git clone, uv venv) so the tests run fast and offline.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from kai.vendors import VENDOR_NAMES
from kai.vendors.ffmpeg import FfmpegVendor
from kai.vendors.kokoro import KokoroVendor
from kai.vendors.manager import VendorManager
from kai.vendors.whisper import WhisperVendor, resolve_model_file, resolve_model_size


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


# --- manager / registry ---


class TestVendorManager:
    def test_resolves_project_root_when_none(self):
        mgr = VendorManager()
        assert (mgr.project_root / "src" / "kai").is_dir()

    def test_known_vendors(self, root):
        mgr = VendorManager(root)
        for name in VENDOR_NAMES:
            assert mgr.get(name).name == name

    def test_unknown_vendor_raises(self, root):
        mgr = VendorManager(root)
        with pytest.raises(KeyError):
            mgr.get("nope")

    def test_list_reports_all_three(self, root):
        rows = VendorManager(root).list()
        assert {r["name"] for r in rows} == set(VENDOR_NAMES)
        for r in rows:
            assert r["installed"] is False
            assert r["version"] is None

    def test_delete_removes_vendor_and_model_dirs(self, root):
        mgr = VendorManager(root)
        v = mgr.get("ffmpeg")
        v.vendor_dir.mkdir(parents=True)
        (v.vendor_dir / "ffmpeg").write_bytes(b"x")
        v.model_dir.mkdir(parents=True)
        (v.model_dir / "m.bin").write_bytes(b"y")
        results = mgr.delete("ffmpeg")
        assert results[0].ok
        assert not v.vendor_dir.exists()
        assert not v.model_dir.exists()

    def test_delete_when_absent_is_ok(self, root):
        results = VendorManager(root).delete("ffmpeg")
        assert results[0].ok
        assert "nothing to remove" in results[0].detail


# --- ffmpeg ---


class TestFfmpegVendor:
    def test_not_installed_by_default(self, root):
        assert not FfmpegVendor(root).is_installed()

    def test_installed_when_binary_present(self, root):
        v = FfmpegVendor(root)
        v.vendor_dir.mkdir(parents=True)
        (v.vendor_dir / "ffmpeg").write_bytes(b"")
        assert v.is_installed()
        assert v.version() == "master-latest"


# --- whisper ---


class TestWhisperVendor:
    def test_model_size_resolution(self):
        assert resolve_model_size("en") == "base"
        assert resolve_model_size("ENGLISH") == "base"
        assert resolve_model_size("auto") == "base"
        assert resolve_model_size("Spanish") == "small"
        assert resolve_model_size("xx-unknown") == "base"

    def test_model_file_resolution(self):
        assert resolve_model_file("base") == "ggml-base.bin"
        assert resolve_model_file("small") == "ggml-small.bin"
        assert resolve_model_file("huge") == "ggml-base.bin"  # fallback

    def test_not_installed_by_default(self, root):
        assert not WhisperVendor(root).is_installed()

    def test_installed_when_cli_present(self, root):
        v = WhisperVendor(root)
        v.vendor_dir.mkdir(parents=True)
        (v.vendor_dir / "whisper-cli").write_bytes(b"")
        assert v.is_installed()

    def test_build_checks_deps(self, root):
        v = WhisperVendor(root)
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="missing build dependencies"):
                v._build()

    def test_download_model_skips_when_present(self, root):
        v = WhisperVendor(root)
        v.model_dir.mkdir(parents=True)
        (v.model_dir / "ggml-base.bin").write_bytes(b"model")
        # remote_size returns 0 (unknown) -> existing file is trusted.
        with mock.patch("kai.vendors.whisper.download") as dl, mock.patch(
            "kai.vendors.whisper.remote_size", return_value=0
        ):
            path = v._download_model("auto")
        assert path.name == "ggml-base.bin"
        dl.assert_not_called()

    def test_download_model_redownloads_when_truncated(self, root):
        v = WhisperVendor(root)
        v.model_dir.mkdir(parents=True)
        (v.model_dir / "ggml-base.bin").write_bytes(b"model")
        # remote reports a size that doesn't match the local stub -> redownload.
        with mock.patch("kai.vendors.whisper.download") as dl, mock.patch(
            "kai.vendors.whisper.remote_size", return_value=147951465
        ):
            v._download_model("auto")
        dl.assert_called_once()


# --- kokoro ---


class TestKokoroVendor:
    def test_not_installed_by_default(self, root):
        assert not KokoroVendor(root).is_installed()

    def test_not_installed_when_venv_lacks_packages(self, root):
        v = KokoroVendor(root)
        v.vendor_dir.mkdir(parents=True)
        (v.vendor_dir / "bin").mkdir()
        (v.vendor_dir / "bin" / "python").write_bytes(b"")
        # _packages_installed runs python -c import... -> returns nonzero
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1)
            assert not v.is_installed()

    def test_installed_when_venv_and_packages(self, root):
        v = KokoroVendor(root)
        v.vendor_dir.mkdir(parents=True)
        (v.vendor_dir / "bin").mkdir()
        (v.vendor_dir / "bin" / "python").write_bytes(b"")
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            assert v.is_installed()
            assert v.version() == "model-v1.0"

    def test_check_uv_raises_when_missing(self, root):
        v = KokoroVendor(root)
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="uv is required"):
                v._check_uv()

    def test_download_model_skips_when_present(self, root):
        v = KokoroVendor(root)
        v.model_dir.mkdir(parents=True)
        (v.model_dir / "kokoro-v1.0.int8.onnx").write_bytes(b"m")
        (v.model_dir / "voices-v1.0.bin").write_bytes(b"v")
        # remote_size returns 0 (unknown) -> existing files are trusted.
        with mock.patch("kai.vendors.kokoro.download") as dl, mock.patch(
            "kai.vendors.kokoro.remote_size", return_value=0
        ):
            model, voices = v._download_model()
        dl.assert_not_called()
        assert model.name == "kokoro-v1.0.int8.onnx"
        assert voices.name == "voices-v1.0.bin"

    def test_download_model_redownloads_when_truncated(self, root):
        v = KokoroVendor(root)
        v.model_dir.mkdir(parents=True)
        (v.model_dir / "kokoro-v1.0.int8.onnx").write_bytes(b"m")
        (v.model_dir / "voices-v1.0.bin").write_bytes(b"v")
        # remote reports mismatched sizes -> both files redownload.
        with mock.patch("kai.vendors.kokoro.download") as dl, mock.patch(
            "kai.vendors.kokoro.remote_size", return_value=999999
        ):
            v._download_model()
        assert dl.call_count == 2


# --- CLI smoke (help only, no network) ---


class TestVendorsCli:
    def test_help_lists_subcommands(self):
        from typer.testing import CliRunner

        from kai.vendors.cli import app

        result = CliRunner().invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "install" in result.output
        assert "update" in result.output
        assert "delete" in result.output
