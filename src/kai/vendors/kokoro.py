"""kokoro TTS vendor — isolated venv + ONNX model + voices.

Ports ``scripts/setup_kokoro.sh``. Creates an isolated uv venv at
``vendor/kokoro/`` (Python 3.13), pip-installs ``kokoro-onnx`` + ``soundfile``
into it (NOT the project venv), and downloads the int8-quantized ONNX model
and voice pack to ``models/kokoro/``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from kai.vendors.download import download
from kai.vendors.manager import Vendor, VendorResult

logger = logging.getLogger(__name__)

PYTHON_VERSION = "3.13"
MODEL_VERSION = "v1.0"
MODEL_FILE = f"kokoro-{MODEL_VERSION}.int8.onnx"
VOICES_FILE = f"voices-{MODEL_VERSION}.bin"
_BASE_URL = (
    f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-{MODEL_VERSION}"
)


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


class KokoroVendor(Vendor):
    name = "kokoro"

    def is_installed(self) -> bool:
        py = _venv_python(self.vendor_dir)
        if not py.is_file():
            return False
        # A venv dir without the packages is half-installed — treat as missing.
        return self._packages_installed()

    def version(self) -> str | None:
        return f"model-{MODEL_VERSION}" if self.is_installed() else None

    def _packages_installed(self) -> bool:
        py = _venv_python(self.vendor_dir)
        if not py.is_file():
            return False
        return (
            subprocess.run(
                [str(py), "-c", "import kokoro_onnx, soundfile"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )

    def _check_uv(self) -> None:
        if not shutil.which("uv"):
            raise RuntimeError(
                "uv is required (https://docs.astral.sh/uv/getting-started/installation/)"
            )

    def _create_venv(self) -> None:
        if _venv_python(self.vendor_dir).is_file():
            logger.info("venv already present at %s — skipping", self.vendor_dir)
            return
        self.vendor_dir.parent.mkdir(parents=True, exist_ok=True)
        self._check_uv()
        logger.info("creating isolated venv at %s (Python %s)", self.vendor_dir, PYTHON_VERSION)
        subprocess.run(
            ["uv", "venv", "--python", PYTHON_VERSION, str(self.vendor_dir)],
            check=True,
        )

    def _install_packages(self) -> None:
        if self._packages_installed():
            logger.info("kokoro-onnx + soundfile already installed — skipping")
            return
        logger.info("installing kokoro-onnx + soundfile into the isolated venv")
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(_venv_python(self.vendor_dir)),
                "kokoro-onnx",
                "soundfile",
            ],
            check=True,
        )

    def _download_model(self) -> tuple[Path, Path]:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        model_path = self.model_dir / MODEL_FILE
        voices_path = self.model_dir / VOICES_FILE
        if not model_path.exists():
            logger.info("downloading %s (~88MB)", MODEL_FILE)
            download(f"{_BASE_URL}/{MODEL_FILE}", model_path)
        else:
            logger.info("model already present at %s — skipping", model_path)
        if not voices_path.exists():
            logger.info("downloading %s (~27MB)", VOICES_FILE)
            download(f"{_BASE_URL}/{VOICES_FILE}", voices_path)
        else:
            logger.info("voices already present at %s — skipping", voices_path)
        return model_path, voices_path

    def install(self) -> VendorResult:
        self._create_venv()
        self._install_packages()
        model, voices = self._download_model()
        logger.info("venv -> %s", _venv_python(self.vendor_dir))
        logger.info("model -> %s", model)
        logger.info("voices -> %s", voices)
        return VendorResult(
            self.name,
            ok=True,
            path=str(_venv_python(self.vendor_dir)),
            detail=f"model={model.name} voices={voices.name}",
        )
