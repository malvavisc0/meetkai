import typer

from kai.cli.style import (
    DIM,
    GL_OK,
    OK,
    WARN,
    console,
    err_line,
    soft_table,
)

deployment_app = typer.Typer(
    name="deployment",
    no_args_is_help=True,
    help="Admin CRUD over deployments (calls the same service as the web).",
)


@deployment_app.command("create")
def deployment_create(
    user: str = typer.Option(..., "--user", help="User email"),
    bot_type: str = typer.Option(..., "--bot", help="Bot type (e.g. waha)"),
    goal: str = typer.Option(..., "--goal", help="Bot goal (required)"),
    language: str = typer.Option(..., "--language", help="Bot language (required)"),
    voice: str = typer.Option("", "--voice", help="Kokoro voice (auto-picked if empty)"),
):
    """Create a new deployment for a user."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.deployments import DeploymentsService
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.email == user).first()
        if not db_user:
            err_line(f"user '{user}' not found")
            raise typer.Exit(1)
        if db_user.is_disabled:
            err_line(f"user '{user}' is disabled")
            raise typer.Exit(1)
        svc = DeploymentsService(db)
        dep = svc.create(db_user, bot_type, goal, language, voice or None)
        console.print(
            f"{GL_OK} [{OK}]created deployment[/{OK}]  id={dep.id} bot_type={dep.bot_type} "
            f"language={dep.language} voice={dep.voice}"
        )
    finally:
        db.close()


@deployment_app.command("list")
def deployment_list(
    user: str = typer.Option(..., "--user", help="User email"),
):
    """List deployments for a user."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.deployments import DeploymentsService
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.email == user).first()
        if not db_user:
            err_line(f"user '{user}' not found")
            raise typer.Exit(1)
        svc = DeploymentsService(db)
        deps = svc.list_for_user(db_user.id)
        if not deps:
            console.print(f"[{DIM}]no deployments for {user}[/{DIM}]")
            return
        table = soft_table(
            ("id", DIM),
            ("bot_type", "magenta"),
            ("status", ""),
            ("run_id", DIM),
            ("goal", ""),
            ("language", ""),
            ("voice", ""),
        )
        for d in deps:
            goal_display = d.goal[:40] + "..." if len(d.goal) > 40 else d.goal
            table.add_row(
                str(d.id),
                d.bot_type,
                d.status,
                d.run_id or "-",
                goal_display,
                d.language,
                d.voice,
            )
        console.print(table)
    finally:
        db.close()


@deployment_app.command("start")
def deployment_start(
    deployment_id: int = typer.Argument(...),
):
    """Start a deployment."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.deployments import (
        ConnectionRequiredError,
        DeploymentsService,
        DeploymentStartupError,
    )

    create_all()
    db = SessionLocal()
    try:
        svc = DeploymentsService(db)
        dep = svc.get(deployment_id)
        if not dep:
            err_line(f"deployment {deployment_id} not found")
            raise typer.Exit(1)
        try:
            svc.start(dep)
            console.print(f"{GL_OK} [{OK}]started deployment[/{OK}]  {deployment_id}")
        except ConnectionRequiredError as exc:
            err_line(str(exc))
            raise typer.Exit(1) from exc
        except DeploymentStartupError as exc:
            err_line(f"startup failed  {exc}")
            raise typer.Exit(1) from exc
    finally:
        db.close()


@deployment_app.command("stop")
def deployment_stop(
    deployment_id: int = typer.Argument(...),
):
    """Stop a deployment."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.deployments import DeploymentsService

    create_all()
    db = SessionLocal()
    try:
        svc = DeploymentsService(db)
        dep = svc.get(deployment_id)
        if not dep:
            err_line(f"deployment {deployment_id} not found")
            raise typer.Exit(1)
        svc.stop(dep)
        console.print(f"{GL_OK} [{OK}]stopped deployment[/{OK}]  {deployment_id}")
    finally:
        db.close()


@deployment_app.command("edit")
def deployment_edit(
    deployment_id: int = typer.Argument(...),
    goal: str = typer.Option(None, "--goal"),
    language: str = typer.Option(None, "--language"),
    voice: str = typer.Option(None, "--voice"),
):
    """Edit deployment fields (partial update)."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.deployments import DeploymentsService

    create_all()
    db = SessionLocal()
    try:
        svc = DeploymentsService(db)
        dep = svc.get(deployment_id)
        if not dep:
            err_line(f"deployment {deployment_id} not found")
            raise typer.Exit(1)
        fields: dict = {}
        if goal is not None:
            fields["goal"] = goal
        if language is not None:
            fields["language"] = language
        if voice is not None:
            fields["voice"] = voice
        if not fields:
            console.print(f"[{WARN}]\u25cf no fields to update[/{WARN}]")
            return
        svc.edit(dep, **fields)
        console.print(f"{GL_OK} [{OK}]updated deployment[/{OK}]  {deployment_id}")
    finally:
        db.close()


@deployment_app.command("delete")
def deployment_delete(
    deployment_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete a deployment (stops it first if running; WhatsApp is left connected)."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.deployments import DeploymentsService

    create_all()
    db = SessionLocal()
    try:
        svc = DeploymentsService(db)
        dep = svc.get(deployment_id)
        if not dep:
            err_line(f"deployment {deployment_id} not found")
            raise typer.Exit(1)
        if not yes:
            confirm = typer.confirm(
                f"Delete deployment {deployment_id} ({dep.bot_type})?"
                " The bot is stopped if running; WhatsApp stays connected."
            )
            if not confirm:
                console.print(f"[{WARN}]\u25cf aborted[/{WARN}]")
                raise typer.Exit(0)
        svc.delete(dep)
        console.print(f"{GL_OK} [{OK}]deleted deployment[/{OK}]  {deployment_id}")
    finally:
        db.close()
