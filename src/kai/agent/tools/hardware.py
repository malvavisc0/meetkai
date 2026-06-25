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


def epaper_sleep(show_screen: bool = True) -> str:
    """Put the panel into low-power sleep, optionally showing a sleep splash first."""
    epd_module = _import_waveshare_epd()
    if epd_module is None:
        return "Error: waveshare_epd not available"

    # Render the sleep splash so the persistent e-paper image reflects the state.
    # render_sleep_screen() already inits, displays, and sleeps the panel, so when
    # it runs there is nothing left to do (and re-issuing sleep would hit a
    # closed SPI descriptor).
    if show_screen:
        render_sleep_screen()
        return "e-Paper display in sleep mode"

    try:
        epd = epd_module.EPD()
        epd.init()  # ensure SPI/GPIO is up before issuing the sleep command
        epd.sleep()
        return "e-Paper display in sleep mode"
    except Exception as exc:
        logger.warning("failed to sleep e-Paper display: %s", exc)
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
    small_font = _load_font(10)

    def _centered(text, y, font):
        w = draw.textlength(text, font=font)
        draw.text(((_EPD_WIDTH - w) // 2, y), text, font=font, fill=0)

    _centered("I'M AWAKE", 28, big_font)
    _centered("what did i miss?", 62, med_font)

    timestamp = datetime.now(UTC).strftime("%H:%M")
    _centered(f"woke @ {timestamp}", 100, small_font)

    # Sun icon, top-right corner.
    sx, sy, sr = 224, 22, 6
    draw.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=0)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = sx + int((sr + 3) * math.cos(rad))
        y1 = sy + int((sr + 3) * math.sin(rad))
        x2 = sx + int((sr + 10) * math.cos(rad))
        y2 = sy + int((sr + 10) * math.sin(rad))
        draw.line((x1, y1, x2, y2), fill=0, width=1)

    return _push_to_epd(canvas)


def _vibe_store() -> Path:
    return _EPD_OUTPUT_DIR.parent / "vibe.json"


def _save_last_vibe(score: int) -> None:
    try:
        import json

        _vibe_store().parent.mkdir(parents=True, exist_ok=True)
        _vibe_store().write_text(json.dumps({"score": score}), encoding="utf-8")
    except OSError:
        logger.debug("failed to save last vibe score", exc_info=True)


def _draw_mood_face(draw, x: int, y: int, score: int, scale: int = 2):
    """Draw a pixel-art mood face at (x, y) based on score range.

    Uses 8-wide bitmap strings; '#' = black pixel, '.' = transparent.
    The face, eyes, and mouth change with the vibe score.
    """
    if score <= 20:
        face = [
            "########",
            "#......#",
            "#.XX.XX#",  # dead X eyes
            "#......#",
            "#......#",
            "#.----.#",  # flat mouth
            "#......#",
            "########",
        ]
    elif score <= 40:
        face = [
            "########",
            "#......#",
            "#\\..../#",  # concerned slanted eyes
            "#......#",
            "#......#",
            "#.----.#",  # flat mouth
            "#......#",
            "########",
        ]
    elif score <= 60:
        face = [
            "########",
            "#......#",
            "#.-..-.#",  # neutral dot eyes
            "#......#",
            "#......#",
            "#.----.#",  # flat mouth
            "#......#",
            "########",
        ]
    elif score <= 80:
        face = [
            "########",
            "#......#",
            "#.^..^.#",  # smug raised eyes
            "#......#",
            "#......#",
            "#.~~..,#",  # smirk mouth
            "#......#",
            "########",
        ]
    else:
        face = [
            "########",
            "#......#",
            "#.OO.OO#",  # wide deranged eyes
            "#......#",
            "#......#",
            "#.####.#",  # open shouting mouth
            "#.####.#",
            "########",
        ]

    for ry, row in enumerate(face):
        for rx, ch in enumerate(row):
            if ch != ".":
                px = x + rx * scale
                py = y + ry * scale
                draw.rectangle(
                    (px, py, px + scale - 1, py + scale - 1),
                    fill=0,
                )


def _wrap_text(draw, text: str, x: int, y: int, font, max_chars: int, line_height: int = 12) -> int:
    """Draw word-wrapped text. Returns the final y position."""
    words = text[: max_chars * 3].split()
    line = ""
    for word in words:
        test = f"{line} {word}".strip()
        if len(test) > max_chars and line:
            draw.text((x, y), line, font=font, fill=0)
            y += line_height
            line = word
        else:
            line = test
    if line:
        draw.text((x, y), line, font=font, fill=0)
        y += line_height
    return y


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
    med_font = _load_font(16)

    _save_last_vibe(score)

    # --- Header: timestamp left, label as an inverted black chip on the right ---
    timestamp = datetime.now(UTC).strftime("%H:%M")
    draw.text((2, 2), f"VIBECHECK @ {timestamp}", font=title_font, fill=0)

    label_text = label.upper()[:20]
    label_w = draw.textlength(label_text, font=title_font)
    chip_pad = 4
    chip_w = int(label_w) + chip_pad * 2
    chip_x0 = _EPD_WIDTH - chip_w - 2
    draw.rounded_rectangle((chip_x0, 0, _EPD_WIDTH - 2, 15), radius=3, fill=0)
    draw.text((chip_x0 + chip_pad, 2), label_text, font=title_font, fill=255)

    draw.line((2, 17, _EPD_WIDTH - 2, 17), fill=0, width=1)

    # --- Body: mood face (left) + wrapped quote (right) ---
    face_scale = 5
    face_w = 8 * face_scale  # 40px
    col_divider_x = face_w + 14
    face_top = 24

    _draw_mood_face(draw, 6, face_top, score, scale=face_scale)

    body_bottom = 90
    draw.line((col_divider_x, 22, col_divider_x, body_bottom), fill=0, width=1)

    quote_x = col_divider_x + 6
    max_chars = (_EPD_WIDTH - quote_x - 4) // 9
    _wrap_text(draw, quote, quote_x, 26, med_font, max_chars, line_height=18)

    # --- Vibe meter across the bottom ---
    _draw_vibe_meter(draw, score, y_top=96)

    return _push_to_epd(canvas)


def _draw_vibe_meter(draw, score: int, y_top: int) -> None:
    """Draw a horizontal vibe gauge: outlined bar with proportional fill + ticks."""
    score = max(0, min(100, score))
    x0, x1 = 8, _EPD_WIDTH - 8
    bar_h = 12
    y_bot = y_top + bar_h

    # Bar outline.
    draw.rectangle((x0, y_top, x1, y_bot), outline=0, width=1)

    # Proportional fill (inset by 1px so it sits inside the outline).
    inner_x0, inner_x1 = x0 + 2, x1 - 2
    fill_w = int((inner_x1 - inner_x0) * score / 100)
    if fill_w > 0:
        draw.rectangle((inner_x0, y_top + 2, inner_x0 + fill_w, y_bot - 2), fill=0)

    # Tick marks below the bar (every 10%).
    tick_y = y_bot + 2
    span = x1 - x0
    for i in range(11):
        tx = x0 + int(span * i / 10)
        tick_len = 4 if i % 5 == 0 else 2
        draw.line((tx, tick_y, tx, tick_y + tick_len), fill=0, width=1)


def _epaper_available() -> bool:
    return epaper_available()
