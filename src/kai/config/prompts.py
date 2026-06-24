import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".prompt"}


def load_system_prompt(filepath: str | Path, variables: dict[str, str] | None = None) -> str:
    path = Path(filepath).resolve()

    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    if not path.is_file():
        raise ValueError(f"Prompt path is not a file: {path}")

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format: {path.suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    try:
        content = path.read_text(encoding="utf-8").strip()
        if variables:
            for key, value in variables.items():
                content = content.replace(f"{{{{{key}}}}}", value)
    except UnicodeDecodeError as exc:
        raise ValueError(f"Prompt file is not valid UTF-8: {path}: {exc}") from exc
    except OSError as exc:
        raise OSError(f"Failed to read prompt file {path}: {exc}") from exc

    if not content:
        raise ValueError(f"Prompt file is empty or whitespace-only: {path}")

    logger.info("Loaded system prompt from %s (%d chars)", path.name, len(content))
    logger.debug("Prompt content (truncated): %.200s", content)
    return content
