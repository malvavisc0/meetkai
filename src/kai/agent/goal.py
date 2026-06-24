import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Goal:
    description: str
    system_prompt: str | None = None


@dataclass
class GoalManager:
    _current: Goal | None = field(default=None, init=False)
    _revision: int = field(default=0, init=False)

    def set_goal(self, description: str) -> Goal:
        self._current = Goal(description=description)
        self._revision += 1
        logger.info("Goal set: %s", description)
        return self._current

    def restore_goal(self, description: str) -> Goal:
        self._current = Goal(description=description)
        logger.info("Goal restored: %s", description)
        return self._current

    def get_goal(self) -> Goal | None:
        return self._current

    def clear_goal(self) -> None:
        if self._current is None:
            return
        logger.info("Goal cleared")
        self._current = None
        self._revision += 1

    def has_goal(self) -> bool:
        return self._current is not None

    @property
    def revision(self) -> int:
        return self._revision
