import logging
import sys
from pathlib import Path

_configured = False
_log_dir_configured: Path | None = None


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    """Configure kai logging: stderr console + optional file handler."""
    global _configured, _log_dir_configured

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("kai")
    root.setLevel(level)
    root.propagate = False

    if not root.handlers:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.setLevel(level)
        root.addHandler(console)

    if log_dir and log_dir != _log_dir_configured:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_dir / "kai.log")
            file_handler.setFormatter(fmt)
            file_handler.setLevel(level)
            root.addHandler(file_handler)

            ignored_handler = logging.FileHandler(log_dir / "ignored_messages.log")
            ignored_handler.setFormatter(fmt)
            ignored_handler.setLevel(logging.INFO)
            ignored_handler.addFilter(
                lambda record: (
                    "blocked" in record.getMessage().lower()
                    or "ignored" in record.getMessage().lower()
                )
            )
            root.addHandler(ignored_handler)
            _log_dir_configured = log_dir
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Failed to set up file logging in %s: %s", log_dir, exc
            )

    if not _configured:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).debug("Logging initialized (level=%s)", logging.getLevelName(level))
