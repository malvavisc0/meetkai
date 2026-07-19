"""ffmpeg vendor — download a prebuilt static ffmpeg + ffprobe.

Ports the ``download_ffmpeg`` function from ``scripts/setup_media.sh``.
Keeps the "latest master" BtbN build URL for Linux (non-reproducible but
always current, per project decision) and evermeet for macOS.

No build step — ffmpeg is distributed as prebuilt binaries.
"""

import logging
import platform
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path

from kai.vendors.download import download
from kai.vendors.manager import Vendor, VendorResult

logger = logging.getLogger(__name__)


def _detect_platform() -> str:
    """Return one of: linux-x64, linux-arm64, macos-arm64, macos-x64."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64"
    if system == "Darwin":
        return "macos-arm64" if machine == "arm64" else "macos-x64"
    raise RuntimeError(f"unsupported OS for ffmpeg: {system}")


def _url_for(platform_id: str) -> str:
    if platform_id == "linux-x64":
        return (
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            "ffmpeg-master-latest-linux64-gpl.tar.xz"
        )
    if platform_id == "linux-arm64":
        return (
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            "ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
        )
    # evermeet ships ffmpeg and ffprobe as separate zips; caller handles ffprobe.
    return "https://evermeet.cx/ffmpeg/ffmpeg-7.1.1.zip"


def _url_ffprobe() -> str:
    return "https://evermeet.cx/ffmpeg/ffprobe-7.1.1.zip"


class FfmpegVendor(Vendor):
    name = "ffmpeg"

    def is_installed(self) -> bool:
        return (self.vendor_dir / "ffmpeg").is_file()

    def version(self) -> str | None:
        # BtbN "latest master" has no embedded version; report the marker.
        return "master-latest" if self.is_installed() else None

    def install(self) -> VendorResult:
        self.vendor_dir.mkdir(parents=True, exist_ok=True)
        if self.is_installed():
            logger.info("ffmpeg already present at %s — skipping", self.vendor_dir / "ffmpeg")
            return VendorResult(
                self.name,
                ok=True,
                path=str(self.vendor_dir / "ffmpeg"),
                detail="already installed",
            )

        plat = _detect_platform()
        logger.info("installing ffmpeg for %s", plat)

        if plat.startswith("macos-"):
            ffmpeg_zip = self.vendor_dir / "ffmpeg.zip"
            download(_url_for(plat), ffmpeg_zip)
            with zipfile.ZipFile(ffmpeg_zip) as z:
                z.extractall(self.vendor_dir)
            ffmpeg_zip.unlink(missing_ok=True)

            ffprobe_zip = self.vendor_dir / "ffprobe.zip"
            download(_url_ffprobe(), ffprobe_zip)
            with zipfile.ZipFile(ffprobe_zip) as z:
                z.extractall(self.vendor_dir)
            ffprobe_zip.unlink(missing_ok=True)
        else:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "ffmpeg.tar.xz"
                download(_url_for(plat), archive)
                with tarfile.open(archive, "r:xz") as tar:
                    tar.extractall(tmp)  # noqa: S202 — archive from trusted source
                # Archive top dir is ffmpeg-*/ — copy its bin/* into vendor_dir.
                extracted = next(Path(tmp).glob("ffmpeg-*/"))
                for name in ("ffmpeg", "ffprobe"):
                    src = extracted / "bin" / name
                    if src.exists():
                        shutil.copy(src, self.vendor_dir / name)

        for name in ("ffmpeg", "ffprobe"):
            p = self.vendor_dir / name
            if p.exists():
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        logger.info("ffmpeg -> %s", self.vendor_dir / "ffmpeg")
        logger.info("ffprobe -> %s", self.vendor_dir / "ffprobe")
        return VendorResult(self.name, ok=True, path=str(self.vendor_dir / "ffmpeg"))
