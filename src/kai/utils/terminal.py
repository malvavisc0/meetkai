import io
import logging

from PIL import Image
from rich.console import Console
from rich.text import Text

logger = logging.getLogger(__name__)


def render_image_pixelated(
    image_bytes: bytes,
    console: Console,
    width: int = 32,
) -> None:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGBA")

        aspect = img.height / img.width
        height = int(width * aspect * 0.5)
        img = img.resize((width, height), Image.Resampling.NEAREST)

        pixels = img.load()
        for y in range(height):
            line = Text()
            for x in range(width):
                r, g, b, a = pixels[x, y]  # type: ignore[misc]
                if a < 128:
                    line.append("  ")
                else:
                    line.append("██", style=f"rgb({r},{g},{b})")
            console.print(line)
    except Exception as exc:
        logger.debug("Failed to render image: %s", exc)
