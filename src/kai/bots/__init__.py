import importlib
import pkgutil
from pathlib import Path

from kai.bots.base import BaseBot

_PKG = __name__  # "kai.bots"


def list_bots() -> list[str]:
    """Return names of all bot subpackages (directories with __init__.py)."""
    return sorted(m.name for m in pkgutil.iter_modules(__path__) if m.ispkg)


def load_bot(name: str) -> BaseBot:
    """Import kai.bots.<name> and return a Bot instance.

    Convention: each kai/bots/<name>/__init__.py exports a `Bot` class
    accepting `bot_dir: Path` (and optional `config=` for testing).
    """
    if name not in list_bots():
        raise ValueError(f"Bot '{name}' not found. Available: {list_bots()}")
    module = importlib.import_module(f"{_PKG}.{name}")
    bot_cls = getattr(module, "Bot", None)
    if bot_cls is None:
        raise ValueError(
            f"Bot '{name}' module has no 'Bot' class. "
            f"Expected kai.bots.{name}.__init__.py to define a class named Bot."
        )
    bot_dir = Path(module.__file__).resolve().parent
    return bot_cls(bot_dir=bot_dir)
