import typer

from kai.cli.bot import register as register_bot_commands
from kai.cli.cockpit import cockpit_app, cockpit_user_app
from kai.cli.connection import connection_app
from kai.cli.deployment import deployment_app
from kai.cli.style import BotStartupError
from kai.cli.templates import build_app as build_templates_app
from kai.observability import init_sentry

init_sentry("cli")

app = typer.Typer(
    name="kai",
    no_args_is_help=True,
    help="Run Kai bots, manage deployments, and operate the cockpit.",
)

register_bot_commands(app)
app.add_typer(cockpit_app, name="cockpit")
app.add_typer(deployment_app, name="deployment")
app.add_typer(connection_app, name="connection")
app.add_typer(build_templates_app(), name="template")
app.add_typer(build_templates_app(), name="templates", hidden=True)

__all__ = ["app", "BotStartupError", "cockpit_user_app"]
