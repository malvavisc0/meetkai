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
    help="List account-level integrations for a user.",
)


@connection_app.command("list")
def connection_list(
    user: str = typer.Option(..., "--user", help="User email"),
):
    """List a user's connections."""
    from kai.cockpit.connections.service import ConnectionsService
    from kai.cockpit.db import SessionLocal
    from kai.cockpit.models import User

    db = SessionLocal()
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
