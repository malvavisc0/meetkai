import logging

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class Goal(BaseModel):
    """A clear, operator-set directive the agent should work toward."""

    model_config = ConfigDict(frozen=True)

    description: str


class GoalManager:
    """Tracks the current goal and its revision count.

    A plain class (not a ``BaseModel``): it's a stateful service object with
    only private attributes and methods, never serialized or validated from
    external data. Using ``BaseModel`` gave it an empty JSON schema and
    unused ``model_validate``/``model_dump`` methods that implied a
    serialization boundary that doesn't exist.
    """

    def __init__(self) -> None:
        self._current: Goal | None = None
        self._revision: int = 0

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
