import typer

from kai.cli.bot import register as _register_bot_commands
from kai.cli.cockpit import cockpit_app, cockpit_user_app
from kai.cli.connection import connection_app
from kai.cli.deployment import deployment_app
from kai.cli.style import BotStartupError
from kai.vendors.cli import app as vendors_app

app = typer.Typer(name="kai", no_args_is_help=True)

_register_bot_commands(app)
app.add_typer(cockpit_app, name="cockpit")
app.add_typer(deployment_app, name="deployment")
app.add_typer(connection_app, name="connection")
app.add_typer(vendors_app, name="vendors")

__all__ = ["app", "BotStartupError", "cockpit_user_app"]
