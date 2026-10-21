import typer
from rich.console import Console

from kai.cli.style import DIM, WARN, soft_table
from kai.config.prompts import load_system_prompt
from kai.templates import TemplateRegistry, escalation_prompt_section
from kai.templates.schema import TemplateDef

REGISTRY = TemplateRegistry.bundled()


def build_app() -> typer.Typer:
    """Build the templates sub-app.

    Called twice at import: once as ``kai template`` (primary) and once as the
    hidden ``kai templates`` alias kept for back-compat. A fresh app per call
    gives each registration its own option objects.
    """
    app = typer.Typer(
        name="template",
        no_args_is_help=True,
        help="Inspect the bundled bot templates: list, show, and preview prompts.",
    )

    @app.command("list")
    def list_cmd(
        transport: str | None = typer.Option(
            None, help="Only show templates for this transport (email)"
        ),
    ) -> None:
        """List the bot templates bundled with Kai."""
        list_templates(transport)

    @app.command("show")
    def show_cmd(
        template_id: str = typer.Argument(..., help="Template identifier (e.g. email/general)"),
    ) -> None:
        """Show a template's tools, settings, and escalation rules."""
        show_template(template_id)

    @app.command("render")
    def render_cmd(
        template_id: str = typer.Argument(..., help="Template identifier (e.g. email/general)"),
        goal: str = typer.Option("", "--goal", "-g", help="Goal to fill in for the preview"),
        language: str = typer.Option(
            "", "--language", "-l", help="Language to fill in for the preview"
        ),
    ) -> None:
        """Print a template's system prompt so you can preview it."""
        render_prompt(template_id, goal, language)

    return app


def list_templates(transport: str | None) -> None:
    """List the bundled templates, optionally filtered by transport."""
    templates = REGISTRY.list(transport)
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


def show_template(template_id: str) -> None:
    """Print a template's tools, settings, and escalation rules."""
    parts = parse_id(template_id)
    try:
        tmpl = REGISTRY.get(parts.transport, parts.name)
    except FileNotFoundError:
        typer.echo(f"Template not found: {template_id}")
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"Invalid template {template_id}: {exc}")
        raise typer.Exit(1) from exc

    render_template(tmpl)


def render_prompt(template_id: str, goal: str, language: str) -> None:
    """Print a template's rendered system prompt."""
    parts = parse_id(template_id)
    try:
        tmpl = REGISTRY.get(parts.transport, parts.name)
    except FileNotFoundError:
        typer.echo(f"Template not found: {template_id}")
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"Invalid template {template_id}: {exc}")
        raise typer.Exit(1) from exc

    prompt_path = REGISTRY.prompt_path(parts.transport, parts.name)
    if prompt_path is None:
        typer.echo(f"No prompt.md found for {template_id}")
        raise typer.Exit(1)

    variables = {
        "goal": goal or tmpl.config.get("goal") or "",
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

    def __init__(self, transport: str, name: str) -> None:
        self.transport = transport
        self.name = name


def parse_id(template_id: str) -> TemplateParts:
    """Parse a ``transport/name`` template id or exit with an error."""
    parts = template_id.split("/")
    if len(parts) != 2:
        typer.echo(
            f"Invalid template id: {template_id!r}. "
            f"Expected format: transport/name (e.g. email/general)"
        )
        raise typer.Exit(1)
    transport, name = parts
    if transport not in ("email",):
        typer.echo(f"Unknown transport: {transport!r}. Valid: email")
        raise typer.Exit(1)
    return TemplateParts(transport=transport, name=name)


def render_template(tmpl: TemplateDef) -> None:
    """Print a template's header, tools, config, and prompt sections."""
    c = Console()

    c.print(f"[bold]{tmpl.display_name}[/bold]  [{DIM}]{tmpl.transport}/{tmpl.name}[/{DIM}]")
    c.print()

    first_line = tmpl.description.strip().split("\n")[0]
    c.print(f"  [{DIM}]{first_line}[/{DIM}]")

    render_template_tools(c, tmpl)
    render_template_config(c, tmpl)
    render_template_sections(c, tmpl)


def render_template_tools(c: Console, tmpl: TemplateDef) -> None:
    """Print a template's actions and required/optional tools."""
    if tmpl.actions:
        c.print("\n  [bold]actions[/bold]  " + ", ".join(tmpl.actions))
    if tmpl.tools.required:
        c.print("  [bold]required tools[/bold]  " + ", ".join(tmpl.tools.required))
    if tmpl.tools.optional:
        c.print("  [bold]optional tools[/bold]  " + ", ".join(tmpl.tools.optional))


def render_template_config(c: Console, tmpl: TemplateDef) -> None:
    """Print a template's configured temperature and language."""
    if not tmpl.config:
        return
    c.print("\n  [bold]config[/bold]")
    temp = tmpl.config.get("temperature")
    if temp is not None:
        c.print(f"    temperature: {temp}")
    la = tmpl.config.get("language")
    if la:
        c.print(f"    language: {la}")


def render_template_sections(c: Console, tmpl: TemplateDef) -> None:
    """Print a template's reply style, escalation rules, and goal hint."""
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
        lines = tmpl.goal_suggestion.strip().split("\n")
        for line in lines[:2]:
            c.print(f"    [{DIM}]{line.strip()}[/{DIM}]")
        if len(lines) > 2:
            c.print(f"    [{DIM}]...[/{DIM}]")
