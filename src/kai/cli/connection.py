import typer

from kai.cli.style import (
    DIM,
    WARN,
    console,
    err_line,
)

connection_app = typer.Typer(
    name="connection",
    no_args_is_help=True,
    help="Show a user's connected integrations.",
)


@connection_app.command("list")
def connection_list(
    user: str = typer.Option(..., "--user", help="User email"),
) -> None:
    """List the integrations a user has connected."""
    from kai.cockpit.connections.service import ConnectionsService
    from kai.cockpit.db import open_session
    from kai.cockpit.models import User

    db = open_session()
    try:
        db_user = db.query(User).filter(User.email == user).first()
        if not db_user:
            err_line(f"user '{user}' not found")
            raise typer.Exit(1)
        conns = ConnectionsService(db).list_for_user(db_user)
        if not conns:
            console.print(f"[{DIM}]no connections for {user}[/{DIM}]")
            return
        for conn in conns:
            console.print(f"[{WARN}]{conn.service}[/{WARN}]  status=[bold]{conn.status}[/bold]")
    finally:
        db.close()
