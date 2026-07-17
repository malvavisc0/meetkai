from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TemplateTools(BaseModel):
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class PostProcessingConfig(BaseModel):
    profile: Literal["waha_default", "none", "custom"] = "none"
    strip_emojis: bool = False
    strip_markdown: bool = False
    collapse_to_single_line: bool = False
    strip_trailing_period: bool = False
    max_sentences: int | None = None
    max_words: int | None = None

    @model_validator(mode="after")
    def _reject_dead_config(self) -> PostProcessingConfig:
        if self.profile == "custom":
            return self

        step_fields = {
            "strip_emojis": self.strip_emojis,
            "strip_markdown": self.strip_markdown,
            "collapse_to_single_line": self.collapse_to_single_line,
            "strip_trailing_period": self.strip_trailing_period,
            "max_sentences": self.max_sentences,
            "max_words": self.max_words,
        }
        set_fields = [name for name, value in step_fields.items() if value not in (False, None)]
        if set_fields:
            raise ValueError(
                f"post_processing.profile={self.profile!r} ignores {set_fields} "
                f"— set profile: custom to use these fields, or remove them."
            )
        return self


class EscalationRule(BaseModel):
    condition: str
    severity: Literal["low", "medium", "high", "critical"]
    message: str


class TemplateDef(BaseModel):
    name: str
    transport: Literal["waha", "email"]
    display_name: str
    description: str
    actions: list[str]
    config: dict[str, Any] = Field(default_factory=dict)
    tools: TemplateTools = Field(default_factory=TemplateTools)
    post_processing: PostProcessingConfig = Field(default_factory=PostProcessingConfig)
    reply_style: str = ""
    goal_suggestion: str = ""
    escalation_rules: list[EscalationRule] = Field(default_factory=list)
    min_version: str = "0.1.0"
