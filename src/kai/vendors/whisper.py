"""whisper.cpp vendor — build whisper-cli + whisper-server + download a model.

Ports ``build_whisper_cpp`` and ``download_model`` from
``scripts/setup_media.sh``. Builds a static whisper.cpp (cmake) so the
binaries are self-contained, then downloads a ggml model sized for the
requested language.

Build deps (cmake, make, gcc) must be present on the host; this module does
not install them — the error surfaces clearly if missing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from kai.vendors.download import download
from kai.vendors.manager import Vendor, VendorResult

logger = logging.getLogger(__name__)

WHISPER_VERSION = os.environ.get("WHISPER_VERSION", "master")
_REPO_URL = "https://github.com/ggml-org/whisper.cpp.git"

# Language -> model size. Matches the shell script's resolve_model_size.
_SIZE_BY_LANGUAGE: dict[str, str] = {
    "en": "base",
    "english": "base",
    "auto": "base",
    "es": "small",
    "spanish": "small",
    "fr": "small",
    "french": "small",
    "de": "small",
    "german": "small",
    "pt": "small",
    "portuguese": "small",
    "it": "small",
    "italian": "small",
    "ru": "small",
    "russian": "small",
    "zh": "small",
    "chinese": "small",
    "ja": "small",
    "japanese": "small",
    "ko": "small",
    "korean": "small",
    "ar": "small",
    "arabic": "small",
    "hi": "small",
    "hindi": "small",
}

_MODEL_FILES = {
    "tiny": "ggml-tiny.bin",
    "base": "ggml-base.bin",
    "small": "ggml-small.bin",
    "medium": "ggml-medium.bin",
    "large": "ggml-large-v3.bin",
}


def resolve_model_size(language: str) -> str:
    return _SIZE_BY_LANGUAGE.get(language.lower(), "base")


def resolve_model_file(size: str) -> str:
    return _MODEL_FILES.get(size, _MODEL_FILES["base"])


class WhisperVendor(Vendor):
    name = "whisper"

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        # The binaries live under vendor/whisper.cpp/ (not vendor/whisper/) to
        # match WahaSettings.whisper_cpp_path's default and the historical
        # layout from scripts/setup_media.sh. Model files stay under
        # models/whisper/ (self.model_dir, unchanged).
        self.vendor_dir = project_root / "vendor" / "whisper.cpp"

    def is_installed(self) -> bool:
        return (self.vendor_dir / "whisper-cli").is_file()

    def version(self) -> str | None:
        return WHISPER_VERSION if self.is_installed() else None

    def _check_build_deps(self) -> None:
        missing = [d for d in ("cmake", "make", "gcc") if not shutil.which(d)]
        if missing:
            raise RuntimeError(f"missing build dependencies: {missing}. Install them and re-run.")

    def _build(self) -> None:
        self._check_build_deps()
        logger.info("building whisper.cpp %s (static)", WHISPER_VERSION)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            logger.info("  cloning whisper.cpp %s", WHISPER_VERSION)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    WHISPER_VERSION,
                    _REPO_URL,
                    str(tmp_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            build_dir = tmp_path / "build"
            logger.info("  configuring (BUILD_SHARED_LIBS=0)")
            subprocess.run(
                [
                    "cmake",
                    "-B",
                    str(build_dir),
                    "-S",
                    str(tmp_path),
                    "-DBUILD_SHARED_LIBS=0",
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DGGML_CCACHE=OFF",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            nproc = os.cpu_count() or 2
            logger.info("  compiling (%d threads)", nproc)
            subprocess.run(
                ["cmake", "--build", str(build_dir), "-j", str(nproc), "--config", "Release"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.vendor_dir.mkdir(parents=True, exist_ok=True)
            for name in ("whisper-cli", "whisper-server"):
                src = build_dir / "bin" / name
                if src.exists():
                    shutil.copy(src, self.vendor_dir / name)
                    (self.vendor_dir / name).chmod(0o755)
            # whisper.cpp historically exposed a `main` symlink; keep it for compat.
            cli = self.vendor_dir / "whisper-cli"
            link = self.vendor_dir / "main"
            if cli.exists() and not link.exists():
                link.symlink_to("whisper-cli")

    def _download_model(self, language: str = "auto") -> Path:
        size = resolve_model_size(language)
        fname = resolve_model_file(size)
        # Honour an explicit override path if set (matches the shell behavior).
        override = os.environ.get("KAI_WAHA_WHISPER_MODEL_PATH", "")
        dest = Path(override) if override else self.model_dir / fname
        if dest.exists():
            logger.info("whisper model already present at %s — skipping", dest)
            return dest
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{fname}"
        logger.info("downloading whisper model %s (lang=%s)", fname, language)
        download(url, dest)
        return dest

    def install(self) -> VendorResult:
        whisper_cli = self.vendor_dir / "whisper-cli"
        whisper_server = self.vendor_dir / "whisper-server"
        if self.is_installed():
            logger.info(f"whisper-cli already present at {whisper_cli} — skipping")
            model = self._download_model()
            return VendorResult(
                self.name,
                ok=True,
                path=str(whisper_cli),
                detail=f"already installed, model={model.name}",
            )
        self._build()
        model = self._download_model()
        logger.info(f"whisper-cli -> {whisper_cli}")
        logger.info(f"whisper-server -> {whisper_server}")
        logger.info(f"whisper model -> {model}")
        return VendorResult(
            self.name,
            ok=True,
            path=str(self.vendor_dir / "whisper-cli"),
            detail=f"model={model.name}",
        )
