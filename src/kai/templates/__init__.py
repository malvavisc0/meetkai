from __future__ import annotations

import logging
from pathlib import Path

import yaml

from kai.templates.schema import TemplateDef

logger = logging.getLogger(__name__)

_TEMPLATE_FILE = "template.yaml"
_PROMPT_FILE = "prompt.md"


class TemplateRegistry:
    def __init__(self, *dirs: Path | str):
        self._dirs = [Path(d) for d in dirs]

    @classmethod
    def bundled(cls) -> TemplateRegistry:
        templates_dir = Path(__file__).resolve().parents[3] / "templates"
        if not templates_dir.is_dir():
            raise FileNotFoundError(f"Templates directory not found: {templates_dir}")
        return cls(templates_dir)

    def list(self, transport: str | None = None) -> list[TemplateDef]:
        templates: list[TemplateDef] = []
        for dir_path in self._dirs:
            if not dir_path.is_dir():
                continue
            transport_names = []
            if transport:
                transport_names = [(transport, dir_path / transport)]
            else:
                for entry in sorted(dir_path.iterdir()):
                    if entry.is_dir():
                        transport_names.append((entry.name, entry))
            for t_name, transport_dir in transport_names:
                if not transport_dir.is_dir():
                    continue
                for entry in sorted(transport_dir.iterdir()):
                    if not entry.is_dir():
                        continue
                    template_path = entry / _TEMPLATE_FILE
                    if not template_path.is_file():
                        continue
                    tmpl = self._load_template_file(template_path, t_name)
                    if tmpl not in templates:
                        templates.append(tmpl)
        return templates

    def get(self, transport: str, name: str) -> TemplateDef:
        for dir_path in self._dirs:
            template_dir = dir_path / transport / name
            template_path = template_dir / _TEMPLATE_FILE
            if template_path.is_file():
                return self._load_template_file(template_path, transport)
        raise FileNotFoundError(f"Template not found: {transport}/{name}")

    def _load_template_file(self, path: Path, transport: str | None) -> TemplateDef:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid template YAML at {path}: expected mapping")
        result = TemplateDef.model_validate(data)
        if transport and result.transport != transport:
            raise ValueError(
                f"Template transport mismatch at {path}: "
                f"expected {transport!r}, got {result.transport!r}"
            )
        return result

    def prompt_path(self, transport: str, name: str) -> Path | None:
        for dir_path in self._dirs:
            prompt_file = dir_path / transport / name / _PROMPT_FILE
            if prompt_file.is_file():
                return prompt_file
        return None


def escalation_prompt_section(template: TemplateDef) -> str:
    """Build the ``## ESCALATION RULES`` block appended to a template's prompt.

    Returns an empty string when the template declares no escalation rules.
    The block is appended to the base prompt (system-prompt step 1, see
    TEMPLATES §5.6) so the rules land before tool instructions and read as
    hard rules the model must follow before choosing its action.
    """
    if not template.escalation_rules:
        return ""
    lines = ["\n\n## ESCALATION RULES (hard — call escalate before replying)\n"]
    for rule in template.escalation_rules:
        lines.append(
            f"- If {rule.condition} → "
            f'escalate(severity="{rule.severity}", '
            f'reason="{rule.message}")\n'
        )
    return "".join(lines)
