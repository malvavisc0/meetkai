"""Vendor management for kai.

Installs, updates, and removes the external binaries and models the WAHA bot
depends on (ffmpeg, whisper.cpp, kokoro TTS). Each vendor is an isolated unit
with its own ``vendor/<name>/`` directory for binaries/venvs and
``models/<name>/`` directory for model files — nothing touches the project
venv or ``pyproject.toml``.
"""

from kai.vendors.manager import VENDOR_NAMES, Vendor, VendorManager, get_vendor_manager

__all__ = ["VENDOR_NAMES", "Vendor", "VendorManager", "get_vendor_manager"]
