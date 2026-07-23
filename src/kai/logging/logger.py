import logging
import sys
from pathlib import Path


def _has_console_handler(root: logging.Logger) -> bool:
    return any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )


def _has_file_handler(root: logging.Logger, log_dir: Path) -> bool:
    return any(
        isinstance(h, logging.FileHandler) and Path(h.baseFilename).parent == log_dir
        for h in root.handlers
    )


def _silence_noisy_loggers() -> None:
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    """Configure kai logging: stderr console + optional file handler.

    Idempotent: console and file handlers are added only when absent for the
    given directory, so repeated calls never duplicate handlers.
    """
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("kai")
    root.setLevel(level)
    root.propagate = False

    if not _has_console_handler(root):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.setLevel(level)
        root.addHandler(console)

    if log_dir and not _has_file_handler(root, log_dir):
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
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Failed to set up file logging in %s: %s", log_dir, exc
            )

    _silence_noisy_loggers()
    logging.getLogger(__name__).debug("Logging initialized (level=%s)", logging.getLevelName(level))


def reset_logging() -> None:
    """Drop all ``kai`` logger handlers so the next ``setup_logging`` re-applies.

    Test use: lets each test redirect logging to a fresh directory regardless of
    whether an earlier test already configured it.
    """
    root = logging.getLogger("kai")
    for handler in list(root.handlers):
        root.removeHandler(handler)
