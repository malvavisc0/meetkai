"""Vendor registry + install/update/delete orchestration.

A ``Vendor`` describes one external dependency: where its binaries live
(``vendor_dir``), where its models live (``model_dir``), and how to
install/update/delete itself. The ``VendorManager`` owns the registry and the
project-root resolution so the CLI and any future caller share one layout.

Layout (relative to project root, matching the shell scripts it replaces):

    vendor/ffmpeg/      ffmpeg + ffprobe binaries
    vendor/whisper.cpp/ whisper-cli + whisper-server binaries
    vendor/kokoro/      isolated uv venv (kokoro-onnx + soundfile)
    models/whisper/     ggml-*.bin model files
    models/kokoro/      kokoro-*.onnx + voices-*.bin

``delete`` removes BOTH the vendor dir and the model dir so a reinstall is
clean — a half-removed vendor (binary gone, stale model present) is the exact
footgun the explicit delete exists to prevent.
"""

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

VENDOR_NAMES: tuple[str, ...] = ("ffmpeg", "whisper", "kokoro")


@dataclass(frozen=True)
class VendorResult:
    """Outcome of an install/update/delete operation."""

    name: str
    ok: bool
    path: str | None = None
    detail: str = ""


class Vendor(ABC):
    """One external dependency. Subclasses implement the heavy lifting."""

    name: str

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.vendor_dir = project_root / "vendor" / self.name
        self.model_dir = project_root / "models" / self.name

    @abstractmethod
    def is_installed(self) -> bool:
        """True if the vendor's primary artifact exists (binary / venv)."""

    @abstractmethod
    def version(self) -> str | None:
        """Installed version string, or None if unknown / not installed."""

    @abstractmethod
    def install(self) -> VendorResult:
        """Idempotent install. Re-fetches/rebuilds if already present."""

    def delete(self) -> VendorResult:
        """Remove the vendor dir AND its model dir."""
        removed: list[str] = []
        for d in (self.vendor_dir, self.model_dir):
            if d.exists():
                shutil.rmtree(d)
                removed.append(str(d))
        if not removed:
            return VendorResult(self.name, ok=True, detail="nothing to remove")
        return VendorResult(self.name, ok=True, detail=f"removed {', '.join(removed)}")


class VendorManager:
    """Registry + project-root owner for all vendors."""

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root or self._resolve_project_root()
        # Lazy import to avoid a circular dependency: each vendor module
        # imports ``Vendor`` from this module at top level.
        from kai.vendors.ffmpeg import FfmpegVendor
        from kai.vendors.kokoro import KokoroVendor
        from kai.vendors.whisper import WhisperVendor

        self._vendors: dict[str, Vendor] = {
            "ffmpeg": FfmpegVendor(self.project_root),
            "whisper": WhisperVendor(self.project_root),
            "kokoro": KokoroVendor(self.project_root),
        }

    @staticmethod
    def _resolve_project_root() -> Path:
        """Resolve the project root from this file's location.

        ``src/kai/vendors/manager.py`` -> project root is three parents up.
        """
        return Path(__file__).resolve().parents[3]

    def get(self, name: str) -> Vendor:
        if name not in self._vendors:
            raise KeyError(f"unknown vendor: {name!r} (known: {VENDOR_NAMES})")
        return self._vendors[name]

    def all_vendors(self) -> list[Vendor]:
        return [self._vendors[n] for n in VENDOR_NAMES]

    def status_rows(self) -> list[dict]:
        """Return a status row per vendor for the CLI table."""
        rows: list[dict] = []
        for v in self.all_vendors():
            installed = v.is_installed()
            rows.append(
                {
                    "name": v.name,
                    "installed": installed,
                    "version": v.version() if installed else None,
                    "vendor_dir": str(v.vendor_dir),
                    "model_dir": str(v.model_dir),
                }
            )
        return rows

    def install(self, name: str) -> list[VendorResult]:
        """Install one vendor, or all if name == 'all'."""
        targets = self.all_vendors() if name == "all" else [self.get(name)]
        return [v.install() for v in targets]

    def update(self, name: str) -> list[VendorResult]:
        """Update = install (vendors are idempotent and re-fetch)."""
        return self.install(name)

    def delete(self, name: str) -> list[VendorResult]:
        targets = self.all_vendors() if name == "all" else [self.get(name)]
        return [v.delete() for v in targets]


def get_vendor_manager() -> VendorManager:
    return VendorManager()
