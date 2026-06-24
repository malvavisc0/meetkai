#!/usr/bin/env python3
"""Quick check: can render_to_epaper find all dependencies on this Pi?"""

import sys
from pathlib import Path

print("== e-Paper tool dependency check ==\n")

# 1. Hardware deps
for pkg in ("RPi.GPIO", "spidev", "gpiozero", "lgpio", "pigpio"):
    try:
        __import__(pkg)
        print(f"  [OK]  {pkg}")
    except ImportError:
        print(f"  [--]  {pkg}  (optional, not installed)")

print()

# 2. waveshare_epd from vendor/waveshare
vendor_lib = Path(__file__).resolve().parent.parent / "vendor" / "waveshare"
print(f"  vendor path: {vendor_lib}")
print(f"  exists:      {vendor_lib.is_dir()}")

if vendor_lib.is_dir() and str(vendor_lib) not in sys.path:
    sys.path.insert(0, str(vendor_lib))

try:
    from waveshare_epd import epd2in13_V2  # noqa: F401

    print("  [OK]  waveshare_epd.epd2in13_V2 imported")
except ImportError as exc:
    print(f"  [FAIL] waveshare_epd import failed: {exc}")

print()

# 3. Pillow
try:
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401

    print("  [OK]  Pillow")
except ImportError:
    print("  [FAIL] Pillow not installed")

print()

# 4. Final verdict — will the tool register?
hw_ok = False
lib_ok = False

try:
    import RPi.GPIO  # noqa: F401
    import spidev  # noqa: F401

    hw_ok = True
except ImportError:
    pass

try:
    from waveshare_epd import epd2in13_V2  # noqa: F401

    lib_ok = True
except ImportError:
    pass

if hw_ok and lib_ok:
    print("==> render_to_epaper WILL be registered as an agent tool")
else:
    print("==> render_to_epaper will NOT register:")
    if not lib_ok:
        print("    [FAIL] waveshare_epd.epd2in13_V2 not importable")
        print("           Run: bash scripts/setup_epaper.sh")
    if not hw_ok:
        print("    [FAIL] RPi.GPIO / spidev not installed")
        print("           Run: uv pip install '.[epaper]'")
