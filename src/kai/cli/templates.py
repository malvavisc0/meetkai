from __future__ import annotations

import typer

from kai.cli.style import DIM, WARN, soft_table
from kai.config.prompts import load_system_prompt
from kai.templates import TemplateRegistry, escalation_prompt_section
from kai.templates.schema import TemplateDef

app = typer.Typer(help="Bot templates")

_REGISTRY = TemplateRegistry.bundled()


@app.command()
def list(
    transport: str | None = typer.Option(None, help="Filter by transport (waha/email)"),
):
    templates = _REGISTRY.list(transport)
    if not templates:
        if transport:
            typer.echo(f"No templates found for transport: {transport}")
        else:
            typer.echo("No templates found")
        return

    table = soft_table(("template", "bold magenta"), ("display", ""), ("desc", DIM))
    for t in templates:
        key = f"{t.transport}/{t.name}"
        table.add_row(key, t.display_name, (t.description or "").split("\n")[0])

    import rich.console

    rich.console.Console().print(table)


@app.command(name="show")
def show(
    template_id: str = typer.Argument(..., help="Template identifier (e.g. waha/general)"),
):
    parts = _parse_id(template_id)
    try:
        tmpl = _REGISTRY.get(parts.transport, parts.name)
    except FileNotFoundError:
        typer.echo(f"Template not found: {template_id}")
        raise typer.Exit(1)

    _render_template(tmpl)


@app.command()
def render(
    template_id: str = typer.Argument(..., help="Template identifier (e.g. waha/general)"),
    goal: str = typer.Option("", "--goal", "-g", help="Goal to inject for preview"),
    language: str = typer.Option("", "--language", "-l", help="Language to inject"),
):
    parts = _parse_id(template_id)
    try:
        tmpl = _REGISTRY.get(parts.transport, parts.name)
    except FileNotFoundError:
        typer.echo(f"Template not found: {template_id}")
        raise typer.Exit(1)

    prompt_path = _REGISTRY.prompt_path(parts.transport, parts.name)
    if prompt_path is None:
        typer.echo(f"No prompt.md found for {template_id}")
        raise typer.Exit(1)

    variables = {
        "language": language or tmpl.config.get("language") or "English",
        "display_name": tmpl.display_name,
    }

    try:
        prompt = load_system_prompt(str(prompt_path), variables=variables)
    except Exception as exc:
        typer.echo(f"Failed to load prompt: {exc}")
        raise typer.Exit(1)

    if tmpl.escalation_rules:
        prompt += escalation_prompt_section(tmpl)

    typer.echo(prompt)


class TemplateParts:
    transport: str
    name: str

    def __init__(self, transport: str, name: str):
        self.transport = transport
        self.name = name


def _parse_id(template_id: str) -> TemplateParts:
    parts = template_id.split("/")
    if len(parts) != 2:
        typer.echo(
            f"Invalid template id: {template_id!r}. "
            f"Expected format: transport/name (e.g. waha/general)"
        )
        raise typer.Exit(1)
    transport, name = parts
    if transport not in ("waha", "email"):
        typer.echo(f"Unknown transport: {transport!r}. Valid: waha, email")
        raise typer.Exit(1)
    return TemplateParts(transport=transport, name=name)


def _render_template(tmpl: TemplateDef) -> None:
    from rich.console import Console

    c = Console()

    c.print(f"[bold]{tmpl.display_name}[/bold]  [{DIM}]{tmpl.transport}/{tmpl.name}[/{DIM}]")
    c.print()

    first_line = tmpl.description.strip().split("\n")[0]
    c.print(f"  [{DIM}]{first_line}[/{DIM}]")

    if tmpl.actions:
        c.print("\n  [bold]actions[/bold]  " + ", ".join(tmpl.actions))

    if tmpl.tools.required:
        c.print("  [bold]required tools[/bold]  " + ", ".join(tmpl.tools.required))
    if tmpl.tools.optional:
        c.print("  [bold]optional tools[/bold]  " + ", ".join(tmpl.tools.optional))

    if tmpl.config:
        c.print("\n  [bold]config[/bold]")
        temp = tmpl.config.get("temperature")
        if temp is not None:
            c.print(f"    temperature: {temp}")
        la = tmpl.config.get("language")
        if la:
            c.print(f"    language: {la}")

    if tmpl.reply_style:
        c.print("\n  [bold]reply_style[/bold]")
        for line in tmpl.reply_style.strip().split("\n")[:4]:
            c.print(f"    [{DIM}]{line.strip()}[/{DIM}]")

    if tmpl.escalation_rules:
        c.print("\n  [bold]escalation_rules[/bold]")
        for rule in tmpl.escalation_rules:
            c.print(f'    [{WARN}]●[/{WARN}] [{rule.severity}] "{rule.condition}"')

    if tmpl.goal_suggestion:
        c.print("\n  [bold]goal_suggestion[/bold]")
        for line in tmpl.goal_suggestion.strip().split("\n")[:2]:
            c.print(f"    [{DIM}]{line.strip()}[/{DIM}]")
        if len(tmpl.goal_suggestion.strip().split("\n")) > 2:
            c.print(f"    [{DIM}]...[/{DIM}]")
