"""Assert that docs/templates/authoring.md exists and references every
TemplateDef field name. Cheap drift guard so the doc stays in sync with the schema."""

from kai.templates.schema import TemplateDef


class TestDocsAuthoring:
    def test_authoring_guide_exists(self):
        from pathlib import Path

        path = Path("docs/templates/authoring.md")
        assert path.is_file(), f"Missing authoring guide: {path}"
        content = path.read_text(encoding="utf-8")
        assert len(content) > 100, "Authoring guide is too short"

    def test_authoring_guide_references_schema_fields(self):
        """Every field in TemplateDef should be mentioned in the authoring guide."""
        from pathlib import Path

        path = Path("docs/templates/authoring.md")
        content = path.read_text(encoding="utf-8")

        schema_fields = TemplateDef.model_fields.keys()
        missing = []
        for field in schema_fields:
            if field not in content:
                missing.append(field)
        assert missing == [], f"Authoring guide missing these TemplateDef fields: {missing}"
