import pytest
from pydantic import ValidationError

from kai.brain.config import BRAIN_TOOL_NAME, BrainSettings, build_brain_workflow_instruction


class TestBrainSettings:
    def test_defaults(self):
        s = BrainSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.base_url == ""
        assert s.lightrag_api_key == ""
        assert s.workspace == "default"
        assert s.crawler_url == ""
        assert s.crawl4ai_token == ""
        assert s.crawl_max_depth == 1
        assert s.crawl_max_pages == 25
        assert s.instruction == ""
        assert s.mandatory is False

    def test_env_prefix_kai_brain(self):
        # KAI_BRAIN_* env vars map to fields (env_prefix)
        s = BrainSettings(
            _env_file=None,  # type: ignore[call-arg]
            base_url="http://lightrag:9621",
            lightrag_api_key="k",
        )
        assert s.base_url == "http://lightrag:9621"
        assert s.lightrag_api_key == "k"

    def test_invalid_base_url_scheme(self):
        with pytest.raises(ValidationError):
            BrainSettings(_env_file=None, base_url="ftp://bad")  # type: ignore[call-arg]

    def test_invalid_base_url_no_host(self):
        with pytest.raises(ValidationError):
            BrainSettings(_env_file=None, base_url="http://")  # type: ignore[call-arg]

    def test_invalid_crawler_url(self):
        with pytest.raises(ValidationError):
            BrainSettings(_env_file=None, crawler_url="not-a-url")  # type: ignore[call-arg]

    def test_negative_max_depth_rejected(self):
        with pytest.raises(ValidationError):
            BrainSettings(_env_file=None, crawl_max_depth=-1)  # type: ignore[call-arg]

    def test_zero_max_pages_rejected(self):
        with pytest.raises(ValidationError):
            BrainSettings(_env_file=None, crawl_max_pages=0)  # type: ignore[call-arg]

    def test_validate_startup_warns_on_missing_keys(self):
        s = BrainSettings(_env_file=None)  # type: ignore[call-arg]
        warnings = s.validate_startup()
        assert any("BASE_URL" in w for w in warnings)
        assert any("LIGHTRAG_API_KEY" in w for w in warnings)
        assert any("CRAWLER_URL" in w for w in warnings)
        assert any("CRAWL4AI_TOKEN" in w for w in warnings)

    def test_validate_startup_clean_when_set(self):
        s = BrainSettings(
            _env_file=None,  # type: ignore[call-arg]
            base_url="http://lightrag:9621",
            lightrag_api_key="k",
            crawler_url="http://crawl4ai:11235",
            crawl4ai_token="t",
        )
        assert s.validate_startup() == []

    def test_base_url_trailing_slash_stripped(self):
        s = BrainSettings(_env_file=None, base_url="http://lightrag:9621/")  # type: ignore[call-arg]
        assert s.base_url == "http://lightrag:9621"

    def test_brain_enabled_requires_base_url_and_api_key(self):
        s = BrainSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.brain_enabled is False
        s = BrainSettings(
            _env_file=None,  # type: ignore[call-arg]
            base_url="http://lightrag:9621",
            lightrag_api_key="k",
        )
        assert s.brain_enabled is True

    def test_workflow_instruction_delegates_to_builder(self):
        s = BrainSettings(_env_file=None, instruction="Ask about pricing", mandatory=True)  # type: ignore[call-arg]
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
