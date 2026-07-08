from kai.agent.goal import GoalManager


class TestGoalManager:
    def test_no_goal_initially(self):
        gm = GoalManager()
        assert gm.has_goal() is False
        assert gm.get_goal() is None
        assert gm.revision == 0

    def test_set_and_get(self):
        gm = GoalManager()
        goal = gm.set_goal("Be a support bot")
        assert gm.has_goal() is True
        current = gm.get_goal()
        assert current is not None
        assert current.description == "Be a support bot"
        assert goal.description == "Be a support bot"
        assert gm.revision == 1

    def test_overwrite_goal(self):
        gm = GoalManager()
        gm.set_goal("First goal")
        gm.set_goal("Second goal")
        current = gm.get_goal()
        assert current is not None
        assert current.description == "Second goal"

    def test_clear_goal(self):
        gm = GoalManager()
        gm.set_goal("Something")
        gm.clear_goal()
        assert gm.has_goal() is False
        assert gm.get_goal() is None
        assert gm.revision == 2

    def test_set_after_clear(self):
        gm = GoalManager()
        gm.set_goal("First")
        gm.clear_goal()
        gm.set_goal("Second")
        current = gm.get_goal()
        assert current is not None
        assert current.description == "Second"

    def test_clear_when_no_goal(self):
        gm = GoalManager()
        gm.clear_goal()
        assert gm.has_goal() is False
        assert gm.revision == 0
