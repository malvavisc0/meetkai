"""Assert that docs/templates/authoring.md exists and references every
TemplateDef field name. Cheap drift guard so the doc stays in sync with the schema."""

from pathlib import Path

from kai.templates.schema import TemplateDef


class TestDocsAuthoring:
    def test_authoring_guide_exists(self):
        path = Path("docs/templates/authoring.md")
        assert path.is_file(), f"Missing authoring guide: {path}"
        content = path.read_text(encoding="utf-8")
        assert len(content) > 100, "Authoring guide is too short"

    def test_authoring_guide_references_schema_fields(self):
        """Every field in TemplateDef should be mentioned in the authoring guide."""
        path = Path("docs/templates/authoring.md")
        content = path.read_text(encoding="utf-8")

        schema_fields = TemplateDef.model_fields.keys()
        missing = [field for field in schema_fields if f"`{field}`" not in content]
        assert missing == [], f"Authoring guide missing these TemplateDef fields: {missing}"
