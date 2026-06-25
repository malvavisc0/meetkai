import logging
import os
import platform
import shutil
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_EPD_WIDTH = 250
_EPD_HEIGHT = 122
_EPD_MAX_COLS = 62
_EPD_MAX_LINES = 10
_EPD_FONT_SIZE = 8
_EPD_LINE_HEIGHT = 10
_EPD_TITLE_HEIGHT = 12
_EPD_OUTPUT_DIR = Path("data/epaper")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WAVESHARE_VENDOR_LIB = _REPO_ROOT / "vendor" / "waveshare"

_MONOSPACE_FONTS = [
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
]


def _find_monospace_font() -> str | None:
    for path in _MONOSPACE_FONTS:
        if os.path.exists(path):
            return path
    return None


def _import_waveshare_epd():
    import sys

    if _WAVESHARE_VENDOR_LIB.is_dir() and str(_WAVESHARE_VENDOR_LIB) not in sys.path:
        sys.path.insert(0, str(_WAVESHARE_VENDOR_LIB))
    try:
        from waveshare_epd import epd2in13_V3

        return epd2in13_V3
    except ImportError:
        return None


def _format_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024.0 or unit == "PB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _memory_info() -> tuple[int | None, int | None]:
    total: int | None = None
    available: int | None = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
    except (FileNotFoundError, ValueError, IndexError, OSError):
        logger.debug("could not read /proc/meminfo", exc_info=True)
    return total, available


def _disk_info() -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage("/")
        return usage.total, usage.free
    except OSError:
        logger.debug("could not read disk usage", exc_info=True)
        return None, None


def _cpu_load_avg() -> str | None:
    try:
        load = os.getloadavg()
        return f"{load[0]:.2f} (1m)"
    except (OSError, AttributeError):
        logger.debug("load average unavailable", exc_info=True)
        return None


def get_hardware_info() -> dict[str, str]:
    info: dict[str, str] = {}

    info["os"] = platform.platform()
    info["python_version"] = platform.python_version()

    arch = platform.machine() or platform.processor()
    if arch:
        info["cpu_architecture"] = arch

    cpu_count = os.cpu_count()
    if cpu_count:
        info["cpu_count"] = str(cpu_count)

    load = _cpu_load_avg()
    if load is not None:
        info["cpu_load"] = load

    mem_total, mem_available = _memory_info()
    if mem_total is not None:
        info["memory_total"] = _format_bytes(mem_total)
    if mem_available is not None:
        info["memory_available"] = _format_bytes(mem_available)

    disk_total, disk_free = _disk_info()
    if disk_total is not None:
        info["disk_total"] = _format_bytes(disk_total)
    if disk_free is not None:
        info["disk_free"] = _format_bytes(disk_free)

    return info


def _load_font(size: int):
    """Load a font at the given size, falling back to the PIL default."""
    from PIL import ImageFont

    font_path = _find_monospace_font()
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _push_to_epd(canvas) -> str:
    """Push a 1-bit PIL image (_EPD_WIDTH x _EPD_HEIGHT) to the panel.

    Falls back to saving a PNG if hardware is unavailable or errors.
    """
    epd_module = _import_waveshare_epd()
    if epd_module is not None:
        try:
            epd = epd_module.EPD()
            epd.init()
            epd.Clear()
            epd.display(epd.getbuffer(canvas))
            epd.sleep()
            return "rendered successfully on e-Paper display"
        except Exception as exc:
            logger.warning("e-Paper hardware error: %s; falling back to PNG", exc)
    else:
        logger.debug("waveshare_epd not installed; falling back to PNG")

    try:
        _EPD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filename = datetime.now(UTC).strftime("%Y%m%d_%H%M%S.png")
        path = _EPD_OUTPUT_DIR / filename
        canvas.save(str(path))
        return f"saved to {path}"
    except OSError as exc:
        return f"Error: failed to save PNG ({exc})"


def render_to_epaper(ascii_art: str, title: str = "") -> str:
    from PIL import Image, ImageDraw

    if not ascii_art or not ascii_art.strip():
        return "Error: ascii_art is empty"

    font = _load_font(_EPD_FONT_SIZE)
    title_font = _load_font(10)

    image = Image.new("1", (_EPD_WIDTH, _EPD_HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    y_offset = 1
    if title:
        draw.text((2, y_offset), title[:20], font=title_font, fill=0)
        y_offset += _EPD_TITLE_HEIGHT

    lines = ascii_art.split("\n")
    if len(lines) > _EPD_MAX_LINES:
        lines = lines[:_EPD_MAX_LINES]

    for line in lines:
        if y_offset > _EPD_HEIGHT - _EPD_LINE_HEIGHT:
            break
        draw.text((2, y_offset), line[:_EPD_MAX_COLS], font=font, fill=0)
        y_offset += _EPD_LINE_HEIGHT

    return _push_to_epd(image)


def epaper_available() -> bool:
    try:
        import RPi.GPIO  # noqa: F401
        import spidev  # noqa: F401
    except ImportError:
        return False

    return _import_waveshare_epd() is not None


def epaper_clear() -> str:
    epd_module = _import_waveshare_epd()
    if epd_module is None:
        return "Error: waveshare_epd not available"
    try:
        epd = epd_module.EPD()
        epd.init()
        epd.Clear()
        epd.sleep()
        return "e-Paper display cleared"
    except Exception as exc:
        return f"Error: failed to clear e-Paper display ({exc})"


def epaper_sleep() -> str:
    epd_module = _import_waveshare_epd()
    if epd_module is None:
        return "Error: waveshare_epd not available"
    try:
        epd = epd_module.EPD()
        epd.sleep()
        return "e-Paper display in sleep mode"
    except Exception as exc:
        return f"Error: failed to sleep e-Paper display ({exc})"


def render_image_to_epaper(image_bytes: bytes, title: str = "") -> str:
    import io

    from PIL import Image, ImageDraw

    if not image_bytes:
        return "Error: image_bytes is empty"

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception as exc:
        return f"Error: failed to open image ({exc})"

    title_h = _EPD_TITLE_HEIGHT if title else 0
    avail_w, avail_h = _EPD_WIDTH, _EPD_HEIGHT - title_h
    side = min(avail_w, int(avail_h * 0.85))
    img = img.resize((side, side), Image.Resampling.LANCZOS)

    img_1bit = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)

    canvas = Image.new("1", (_EPD_WIDTH, _EPD_HEIGHT), 255)
    x_off = (avail_w - side) // 2
    y_off = title_h + (avail_h - side) // 2
    canvas.paste(img_1bit, (x_off, y_off))

    if title:
        font = _load_font(10)
        ImageDraw.Draw(canvas).text((2, 1), title[:20], font=font, fill=0)

    return _push_to_epd(canvas)


def render_sleep_screen() -> str:
    """Render a 'do not disturb / sleeping' screen to the e-Paper."""

    from PIL import Image, ImageDraw

    canvas = Image.new("1", (_EPD_WIDTH, _EPD_HEIGHT), 255)
    draw = ImageDraw.Draw(canvas)

    big_font = _load_font(24)
    med_font = _load_font(12)
    small_font = _load_font(10)

    cx, cy, r = 190, 30, 18
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=0)
    draw.ellipse((cx - r + 8, cy - r - 2, cx + r + 8, cy + r - 2), fill=255)

    for sx, sy in [(160, 15), (175, 50), (220, 18), (155, 35), (225, 45)]:
        draw.point((sx, sy), fill=0)
        draw.point((sx + 1, sy), fill=0)

    draw.text((10, 15), "Z z z . . .", font=big_font, fill=0)
    draw.text((10, 55), "do not disturb", font=med_font, fill=0)
    draw.text((10, 72), "i am sleeping", font=med_font, fill=0)
    draw.text((10, 105), "(come back later)", font=small_font, fill=0)

    return _push_to_epd(canvas)


def render_wake_screen() -> str:
    """Render an 'I'm awake' splash to the e-Paper."""
    import math

    from PIL import Image, ImageDraw

    canvas = Image.new("1", (_EPD_WIDTH, _EPD_HEIGHT), 255)
    draw = ImageDraw.Draw(canvas)

    big_font = _load_font(24)
    med_font = _load_font(12)

    draw.text((40, 30), "I'M AWAKE", font=big_font, fill=0)
    draw.text((80, 65), "what did i miss?", font=med_font, fill=0)

    sx, sy, sr = 210, 40, 6
    draw.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=0)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = sx + int((sr + 3) * math.cos(rad))
        y1 = sy + int((sr + 3) * math.sin(rad))
        x2 = sx + int((sr + 10) * math.cos(rad))
        y2 = sy + int((sr + 10) * math.sin(rad))
        draw.line((x1, y1, x2, y2), fill=0, width=1)

    return _push_to_epd(canvas)


def render_vibe_check(score: int, label: str, quote: str) -> str:
    """Render a vibe meter to the e-Paper.

    Args:
        score: 0-100 vibe intensity.
        label: one-word label (CHAOTIC, WHOLESOME, DERANGED, ...).
        quote: short description of the energy (max ~60 chars).
    """
    from PIL import Image, ImageDraw

    canvas = Image.new("1", (_EPD_WIDTH, _EPD_HEIGHT), 255)
    draw = ImageDraw.Draw(canvas)

    title_font = _load_font(12)
    big_font = _load_font(24)
    med_font = _load_font(10)
    small_font = _load_font(8)

    draw.text((2, 1), "VIBE CHECK", font=title_font, fill=0)
    draw.line((2, 14, _EPD_WIDTH - 2, 14), fill=0, width=1)

    bar_x, bar_y, bar_w, bar_h = 2, 22, _EPD_WIDTH - 4, 16
    draw.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), outline=0, width=1)
    fill_w = int(bar_w * max(0, min(100, score)) / 100)
    if fill_w > 0:
        draw.rectangle((bar_x + 1, bar_y + 1, bar_x + fill_w, bar_y + bar_h - 1), fill=0)

    draw.text((2, 44), f"{score}%", font=big_font, fill=0)
    label_text = label.upper()[:20]
    label_w = draw.textlength(label_text, font=big_font)
    draw.text((_EPD_WIDTH - label_w - 2, 44), label_text, font=big_font, fill=0)

    y = 80
    max_chars = 50
    words = quote[: max_chars * 2].split()
    line = ""
    for word in words:
        test = f"{line} {word}".strip()
        if len(test) > max_chars and line:
            draw.text((2, y), line, font=med_font, fill=0)
            y += 12
            line = word
        else:
            line = test
    if line:
        draw.text((2, y), line, font=med_font, fill=0)

    draw.text((2, 112), "vibe@" + datetime.now(UTC).strftime("%H:%M"), font=small_font, fill=0)

    return _push_to_epd(canvas)


def _epaper_available() -> bool:
    return epaper_available()
