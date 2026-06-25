import logging
import os
import platform
import shutil
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_EPD_WIDTH = 122
_EPD_HEIGHT = 250
_EPD_MAX_COLS = 62
_EPD_MAX_LINES = 17
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
        from waveshare_epd import epd2in13_V2

        return epd2in13_V2
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


def render_to_epaper(ascii_art: str, title: str = "") -> str:
    from PIL import Image, ImageDraw, ImageFont

    if not ascii_art or not ascii_art.strip():
        return "Error: ascii_art is empty"

    font_path = _find_monospace_font()
    try:
        font = (
            ImageFont.truetype(font_path, _EPD_FONT_SIZE) if font_path else ImageFont.load_default()
        )
    except OSError:
        font = ImageFont.load_default()

    title_font = font
    if font_path:
        try:
            title_font = ImageFont.truetype(font_path, 10)
        except OSError:
            pass

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

    epd_module = _import_waveshare_epd()
    if epd_module is not None:
        try:
            epd = epd_module.EPD()
            epd.init(epd_module.EPD.FULL_UPDATE)
            epd.Clear()
            epd.display(epd.getbuffer(image))
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
        image.save(str(path))
        return f"saved to {path}"
    except OSError as exc:
        return f"Error: failed to save PNG ({exc})"


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
        epd.init(epd_module.EPD.FULL_UPDATE)
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

    from PIL import Image, ImageDraw, ImageFont

    if not image_bytes:
        return "Error: image_bytes is empty"

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception as exc:
        return f"Error: failed to open image ({exc})"

    title_h = _EPD_TITLE_HEIGHT if title else 0
    max_w, max_h = _EPD_WIDTH, _EPD_HEIGHT - title_h
    side = min(max_w, max_h)
    img = img.resize((side, side), Image.Resampling.LANCZOS)

    img_1bit = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)

    canvas = Image.new("1", (_EPD_WIDTH, _EPD_HEIGHT), 255)
    x_off = (_EPD_WIDTH - side) // 2
    y_off = title_h + (max_h - side) // 2
    canvas.paste(img_1bit, (x_off, y_off))

    if title:
        font_path = _find_monospace_font()
        if font_path:
            try:
                font = ImageFont.truetype(font_path, 10)
                ImageDraw.Draw(canvas).text((2, 1), title[:20], font=font, fill=0)
            except OSError:
                pass

    epd_module = _import_waveshare_epd()
    if epd_module is not None:
        try:
            epd = epd_module.EPD()
            epd.init(epd_module.EPD.FULL_UPDATE)
            epd.Clear()
            epd.display(epd.getbuffer(canvas))
            epd.sleep()
            return "rendered image successfully on e-Paper display"
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


def _epaper_available() -> bool:
    return epaper_available()
