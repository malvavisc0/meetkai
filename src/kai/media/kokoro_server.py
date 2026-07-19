"""Kokoro TTS HTTP server — stdlib ``http.server`` shim wrapping kokoro-onnx.

Run under the isolated ``vendor/kokoro`` venv so onnxruntime stays out of the
main process.  Loads the ONNX model once at boot and exposes:

    GET  /health      -> 200 once the model is loaded
    POST /synthesize  -> JSON {text, voice, lang, speed} -> audio/wav bytes

Concurrency: single worker + a ``threading.Lock``.  kokoro-onnx synthesis is
CPU-bound and not assumed thread-safe; requests are serialized.  Adequate for
personal-scale bots.

Usage (standalone, for testing):

    vendor/kokoro/bin/python src/kai/media/kokoro_server.py \\
        --model models/kokoro/kokoro-v1.0.int8.onnx \\
        --voices models/kokoro/voices-v1.0.bin \\
        --host 127.0.0.1 --port 8788
"""

import argparse
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

_kokoro = None
_lock = threading.Lock()
_model_ready = False


def _load_model(model_path: str, voices_path: str) -> None:
    global _kokoro, _model_ready
    from kokoro_onnx import Kokoro  # type: ignore[import-untyped]

    logger.info("loading kokoro model from %s", model_path)
    _kokoro = Kokoro(model_path, voices_path)
    _model_ready = True
    logger.info("kokoro model loaded")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: N803
        logger.debug(format, *args)

    def do_GET(self) -> None:
        if self.path == "/health":
            if _model_ready:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"status":"loading"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self.send_response(404)
            self.end_headers()
            return

        if not _model_ready:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error":"model not loaded"}')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid JSON"}')
            return

        text = body.get("text", "")
        voice = body.get("voice", "af_heart")
        lang = body.get("lang", "en-us")
        speed = float(body.get("speed", 1.0))

        if not text:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"text is required"}')
            return

        try:
            import soundfile as sf  # type: ignore[import-untyped]

            with _lock:
                assert _kokoro is not None
                samples, sr = _kokoro.create(text, voice=voice, speed=speed, lang=lang)
            buf = io.BytesIO()
            sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
            wav_bytes = buf.getvalue()
        except Exception:
            logger.exception("synthesis failed")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"synthesis failed"}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.end_headers()
        self.wfile.write(wav_bytes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kokoro TTS HTTP server")
    parser.add_argument("--model", required=True, help="Path to kokoro ONNX model")
    parser.add_argument("--voices", required=True, help="Path to voices .bin file")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8788, help="Bind port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    _load_model(args.model, args.voices)

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    logger.info("kokoro server listening on %s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
