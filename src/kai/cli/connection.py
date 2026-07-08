import asyncio

import typer

from kai.cli.style import (
    DIM,
    GL_OK,
    OK,
    WARN,
    console,
    err_line,
)

connection_app = typer.Typer(
    name="connection",
    no_args_is_help=True,
    help="Manage account-level integrations (WhatsApp connect, status, disconnect).",
)


@connection_app.command("connect")
def connection_connect(
    service: str = typer.Argument(..., help="Service to connect (e.g. 'whatsapp')"),
    user: str = typer.Option(..., "--user", help="User email"),
):
    """Connect a service for a user."""
    from kai.cockpit.connections import ConnectionsService
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.email == user).first()
        if not db_user:
            err_line(f"user '{user}' not found")
            raise typer.Exit(1)
        if service != "whatsapp":
            err_line(f"unsupported service: {service}")
            raise typer.Exit(1)
        svc = ConnectionsService(db)
        console.print(f"[{DIM}]connecting {service} for {user}...[/{DIM}]")
        result = asyncio.run(svc.connect_whatsapp(db_user))
        status = result.get("status", "unknown")
        if status == "connected":
            console.print(f"{GL_OK} [{OK}]{service} connected[/{OK}]  {user}")
        elif status == "scan_qr":
            console.print(f"[{WARN}]\u25cf scan QR code to complete {service} connection[/{WARN}]")
            console.print(
                f"[{DIM}]CLI cannot display QR images \u2014 use the web UI at /connections[/{DIM}]"
            )
        else:
            console.print(f"[{WARN}]\u25cf connection status: {status}[/{WARN}]")
    finally:
        db.close()


@connection_app.command("status")
def connection_status(
    user: str = typer.Option(..., "--user", help="User email"),
):
    """Show connection status for a user."""
    from kai.cockpit.connections import ConnectionsService
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.email == user).first()
        if not db_user:
            err_line(f"user '{user}' not found")
            raise typer.Exit(1)
        svc = ConnectionsService(db)
        conn = asyncio.run(svc.refresh_status(db_user))
        if not conn:
            console.print(f"[{DIM}]no connections for {user}[/{DIM}]")
            return
        color = OK if conn.status == "connected" else WARN
        console.print(
            f"[{color}]{conn.service}[/{color}]  status=[bold]{conn.status}[/bold]  "
            f"[{DIM}]config={conn.config}[/{DIM}]"
        )
    finally:
        db.close()


@connection_app.command("disconnect")
def connection_disconnect(
    service: str = typer.Argument(..., help="Service to disconnect (e.g. 'whatsapp')"),
    user: str = typer.Option(..., "--user", help="User email"),
):
    """Disconnect a service for a user."""
    from kai.cockpit.connections import ConnectionsService
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.email == user).first()
        if not db_user:
            err_line(f"user '{user}' not found")
            raise typer.Exit(1)
        if service != "whatsapp":
            err_line(f"unsupported service: {service}")
            raise typer.Exit(1)
        svc = ConnectionsService(db)
        asyncio.run(svc.disconnect_whatsapp(db_user))
        console.print(f"{GL_OK} [{OK}]{service} disconnected[/{OK}]  {user}")
    finally:
        db.close()
