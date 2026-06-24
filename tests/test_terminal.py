import io

from PIL import Image
from rich.console import Console

from kai.utils.terminal import render_image_pixelated


def _make_image(width: int = 10, height: int = 10, color: tuple = (255, 0, 0)) -> bytes:
    img = Image.new("RGBA", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestRenderImagePixelated:
    def test_renders_without_error(self):
        console = Console(force_terminal=False, file=io.StringIO())
        image_bytes = _make_image(10, 10, (255, 0, 0))
        render_image_pixelated(image_bytes, console, width=4)
        output = console.file.getvalue()
        assert len(output) > 0

    def test_renders_with_transparency(self):
        console = Console(force_terminal=False, file=io.StringIO())
        image_bytes = _make_image(10, 10, (255, 0, 0, 0))
        render_image_pixelated(image_bytes, console, width=4)
        output = console.file.getvalue()
        assert "  " in output

    def test_handles_invalid_bytes(self):
        console = Console(force_terminal=False, file=io.StringIO())
        render_image_pixelated(b"not an image", console, width=4)
        output = console.file.getvalue()
        assert output == ""

    def test_renders_different_widths(self):
        image_bytes = _make_image(20, 20, (0, 128, 255))
        outputs = {}
        for w in [8, 16, 32]:
            buf = io.StringIO()
            c = Console(force_terminal=False, file=buf)
            render_image_pixelated(image_bytes, c, width=w)
            outputs[w] = buf.getvalue()
        assert outputs[8] != outputs[16]
        assert outputs[16] != outputs[32]

    def test_renders_non_square_image(self):
        console = Console(force_terminal=False, file=io.StringIO())
        image_bytes = _make_image(40, 20, (0, 255, 0))
        render_image_pixelated(image_bytes, console, width=8)
        output = console.file.getvalue()
        lines = output.strip().splitlines()
        assert len(lines) > 0

    def test_renders_to_string(self):
        console = Console(force_terminal=True, file=io.StringIO())
        image_bytes = _make_image(4, 4, (255, 128, 0))
        render_image_pixelated(image_bytes, console, width=2)
        output = console.file.getvalue()
        assert len(output) > 0
