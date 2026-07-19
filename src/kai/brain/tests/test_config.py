import pytest
from pydantic import ValidationError

from kai.brain.config import BRAIN_TOOL_NAME, BrainSettings, build_brain_workflow_instruction


class TestBrainSettings:
    def test_defaults(self):
        s = BrainSettings.for_test()
        assert s.base_url == ""
        assert s.morphik_token == ""
        assert s.workspace == "default"
        assert s.crawler_url == ""
        assert s.crawl4ai_token == ""
        assert s.crawl_max_depth == 1
        assert s.crawl_max_pages == 25
        assert s.instruction == ""
        assert s.mandatory is False

    def test_env_prefix_kai_brain(self):
        # KAI_BRAIN_* env vars map to fields (env_prefix)
        s = BrainSettings.for_test(base_url="http://morphik:8000", morphik_token="k")
        assert s.base_url == "http://morphik:8000"
        assert s.morphik_token == "k"

    def test_invalid_base_url_scheme(self):
        with pytest.raises(ValidationError):
            BrainSettings.for_test(base_url="ftp://bad")

    def test_invalid_base_url_no_host(self):
        with pytest.raises(ValidationError):
            BrainSettings.for_test(base_url="http://")

    def test_invalid_crawler_url(self):
        with pytest.raises(ValidationError):
            BrainSettings.for_test(crawler_url="not-a-url")

    def test_negative_max_depth_rejected(self):
        with pytest.raises(ValidationError):
            BrainSettings.for_test(crawl_max_depth=-1)

    def test_zero_max_pages_rejected(self):
        with pytest.raises(ValidationError):
            BrainSettings.for_test(crawl_max_pages=0)

    def test_validate_startup_warns_on_missing_keys(self):
        s = BrainSettings.for_test()
        warnings = s.validate_startup()
        assert any("BASE_URL" in w for w in warnings)
        assert any("MORPHIK_TOKEN" in w for w in warnings)
        assert any("CRAWLER_URL" in w for w in warnings)
        assert any("CRAWL4AI_TOKEN" in w for w in warnings)

    def test_validate_startup_clean_when_set(self):
        s = BrainSettings.for_test(
            base_url="http://morphik:8000",
            morphik_token="k",
            crawler_url="http://crawl4ai:11235",
            crawl4ai_token="t",
        )
        assert s.validate_startup() == []

    def test_base_url_trailing_slash_stripped(self):
        s = BrainSettings.for_test(base_url="http://morphik:8000/")
        assert s.base_url == "http://morphik:8000"

    def test_brain_enabled_requires_base_url_and_api_key(self):
        s = BrainSettings.for_test()
        assert s.brain_enabled is False
        s = BrainSettings.for_test(base_url="http://morphik:8000", morphik_token="k")
        assert s.brain_enabled is True

    def test_workflow_instruction_delegates_to_builder(self):
        s = BrainSettings.for_test(instruction="Ask about pricing", mandatory=True)
        assert s.workflow_instruction() == build_brain_workflow_instruction(
            "Ask about pricing", True
        )


class TestBuildBrainWorkflowInstruction:
    def test_empty_instruction_still_has_general_awareness(self):
        text = build_brain_workflow_instruction("", False)
        assert text  # never empty — the agent must always know the Brain exists
        assert "Brain" in text
        assert BRAIN_TOOL_NAME in text

    def test_blank_lines_only_treated_as_empty(self):
        text = build_brain_workflow_instruction("   \n\n  ", False)
        assert BRAIN_TOOL_NAME in text
        assert "MUST" not in text
        assert "SHOULD" not in text

    def test_soft_verb_when_not_mandatory(self):
        text = build_brain_workflow_instruction("How to do X from section Y", False)
        assert "SHOULD" in text
        assert "MUST" not in text
        assert "- How to do X from section Y" in text

    def test_hard_verb_when_mandatory(self):
        text = build_brain_workflow_instruction("How to do X from section Y", True)
        assert "MUST" in text
        assert "SHOULD" not in text

    def test_multiple_triggers_bulleted(self):
        text = build_brain_workflow_instruction(
            "product pricing\n\nrefund policy\n  \nsupport hours", True
        )
        assert "- product pricing" in text
        assert "- refund policy" in text
        assert "- support hours" in text

    def test_tool_name_consistent_with_constant(self):
        text = build_brain_workflow_instruction("anything", False)
        assert f"`{BRAIN_TOOL_NAME}`" in text
