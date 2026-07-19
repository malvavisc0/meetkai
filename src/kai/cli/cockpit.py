from datetime import UTC, datetime

import typer

from kai.cli.style import (
    DIM,
    GL_ARROW,
    GL_OK,
    OK,
    WARN,
    _relative,
    console,
    err_line,
    soft_table,
)

cockpit_app = typer.Typer(
    name="cockpit",
    no_args_is_help=True,
    help="Run and manage the kai cockpit web app + its operators.",
)

cockpit_user_app = typer.Typer(
    name="user",
    no_args_is_help=True,
    help="Create, list, and disable cockpit operators (admin only).",
)
cockpit_app.add_typer(cockpit_user_app, name="user")

cockpit_request_app = typer.Typer(
    name="request",
    no_args_is_help=True,
    help="List, create, and approve magic-link login requests (admin only).",
)
cockpit_app.add_typer(cockpit_request_app, name="request")


@cockpit_app.command("serve")
def cockpit_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
    init_db: bool = typer.Option(False, "--init-db", help="Force create_all()"),
):
    """Launch the cockpit web UI (uvicorn)."""
    import uvicorn

    from kai.cockpit.db import create_all
    from kai.logging.logger import setup_logging

    setup_logging()
    create_all()
    if init_db:
        console.print(f"{GL_OK} [{OK}]database tables created[/{OK}]")
    console.print(f"[bold]kai cockpit[/bold] {GL_ARROW} {host}:{port}")
    app_obj = __import__("kai.cockpit.app", fromlist=["create_app"]).create_app()
    uvicorn.run(app_obj, host=host, port=port)


@cockpit_app.command("rotate-credential-key")
def cockpit_rotate_credential_key():
    """Rotate the credential encryption key (run on demand).

    Bumps ``KAI_CREDENTIAL_KEY_VERSION``, re-encrypts every stored credential
    Connection under the new derived key, and updates ``.env``. The root
    secret (``KAI_CREDENTIAL_ENCRYPTION_KEY``) is never touched.
    """
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.key_rotation import rotate_credential_key

    create_all()
    db = SessionLocal()
    try:
        try:
            new_version, env_written = rotate_credential_key(db)
        except RuntimeError as exc:
            err_line(str(exc))
            raise typer.Exit(1) from exc

        console.print(f"{GL_OK} [{OK}]credential key rotated to {new_version}[/{OK}]")
        console.print("Restart the cockpit to apply.")
        if not env_written:
            console.print(
                f"[{WARN}].env not found — set "
                f"KAI_CREDENTIAL_KEY_VERSION={new_version} in your "
                f"environment (docker-compose, systemd, etc.) before restart.[/{WARN}]"
            )
    finally:
        db.close()


@cockpit_user_app.command("create")
def cockpit_user_create(
    email: str = typer.Argument(..., help="Email or username"),
    language: str = typer.Option(..., "--language", help="Default language for deployments"),
    timezone: str = typer.Option(..., "--timezone", help="IANA timezone (e.g. Europe/Berlin)"),
):
    """Create a new cockpit user."""
    import secrets

    from kai.cockpit.bots import ALL_LANGUAGES
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User
    from kai.cockpit.naming import kai_slug_for

    if language not in ALL_LANGUAGES:
        console.print(
            f"[{WARN}]\u25cf unsupported language '{language}'. "
            f"Supported: {', '.join(ALL_LANGUAGES)}[/{WARN}]"
        )
        raise typer.Exit(1)

    create_all()
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            console.print(
                f"[{WARN}]\u25cf user '{email}' already exists (id={existing.id})[/{WARN}]"
            )
            raise typer.Exit(1)
        user = User(
            email=email,
            language=language,
            timezone=timezone,
            hmac_key=secrets.token_hex(32),
            feature_flags={},
            created_at=datetime.now(UTC).isoformat(),
            kai_slug=kai_slug_for(email),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        console.print(f"{GL_OK} [{OK}]created user[/{OK}]  id={user.id} email={user.email}")
        console.print(
            f"[{DIM}]All feature flags are off by default. Grant capabilities with:[/{DIM}]\n"
            f"[{DIM}]  kai cockpit user flags {email} --image --stt --tts --video[/{DIM}]"
        )
    finally:
        db.close()


@cockpit_user_app.command("list")
def cockpit_user_list():
    """List all cockpit users."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        users = db.query(User).all()
        if not users:
            console.print(f"[{DIM}]no users found[/{DIM}]")
            return
        table = soft_table(
            ("id", DIM),
            ("email", "magenta"),
            ("language", ""),
            ("timezone", ""),
            ("disabled", ""),
            ("flags", DIM),
            ("created", DIM),
        )
        for u in users:
            flags = u.feature_flags or {}
            on_flags = [k for k in ("image", "stt", "tts", "video", "sso") if flags.get(k)]
            table.add_row(
                str(u.id),
                u.email,
                u.language,
                u.timezone,
                f"[{WARN}]yes[/{WARN}]" if u.is_disabled else f"[{DIM}]no[/{DIM}]",
                ",".join(on_flags) if on_flags else "-",
                _relative(u.created_at),
            )
        console.print(table)
    finally:
        db.close()


@cockpit_user_app.command("disable")
def cockpit_user_disable(
    email: str = typer.Argument(...),
):
    """Disable a cockpit user."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            err_line(f"user '{email}' not found")
            raise typer.Exit(1)
        user.is_disabled = True
        db.commit()
        console.print(f"{GL_OK} [{OK}]disabled user[/{OK}]  {email}")
    finally:
        db.close()


_USER_FLAGS: tuple[str, ...] = ("image", "stt", "tts", "video", "sso")


@cockpit_user_app.command("flags")
def cockpit_user_flags(
    email: str = typer.Argument(..., help="User email"),
    image: bool = typer.Option(None, "--image/--no-image", help="Toggle image interpretation"),
    video: bool = typer.Option(None, "--video/--no-video", help="Toggle video support"),
    stt: bool = typer.Option(None, "--stt/--no-stt", help="Toggle speech-to-text"),
    tts: bool = typer.Option(None, "--tts/--no-tts", help="Toggle text-to-speech"),
    sso: bool = typer.Option(None, "--sso/--no-sso", help="Toggle SSO login"),
    show: bool = typer.Option(False, "--show", help="Only print current flags, change nothing"),
):
    """Grant or revoke feature-flag entitlements for a user.

    Flags default to OFF on user creation. Pass ``--image`` to enable,
    ``--no-image`` to disable. Omitting a flag leaves it unchanged. With
    ``--show`` the current entitlements are printed without modifying.
    """
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            err_line(f"user '{email}' not found")
            raise typer.Exit(1)

        flags = dict(user.feature_flags or {})
        if not show:
            toggles = {"image": image, "video": video, "stt": stt, "tts": tts, "sso": sso}
            for name, val in toggles.items():
                if val is not None:
                    flags[name] = val
            user.feature_flags = flags
            db.commit()
            db.refresh(user)

        table = soft_table(("flag", ""), ("enabled", ""))
        for name in _USER_FLAGS:
            on = bool(flags.get(name, False))
            table.add_row(name, f"[{OK}]on[/{OK}]" if on else f"[{DIM}]off[/{DIM}]")
        console.print(table)
    finally:
        db.close()


@cockpit_request_app.command("list")
def cockpit_request_list():
    """List pending login requests."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import LoginRequest, User

    create_all()
    db = SessionLocal()
    try:
        requests = db.query(LoginRequest).filter(LoginRequest.status == "pending").all()
        if not requests:
            console.print(f"[{DIM}]no pending requests[/{DIM}]")
            return
        table = soft_table(("id", DIM), ("user email", "magenta"), ("created", DIM))
        for r in requests:
            user = db.query(User).filter(User.id == r.user_id).first()
            email = user.email if user else "?"
            table.add_row(str(r.id), email, _relative(r.created_at))
        console.print(table)
    finally:
        db.close()


@cockpit_request_app.command("create")
def cockpit_request_create(
    email: str = typer.Argument(...),
):
    """Admin: seed a login request for a user (no browser needed)."""
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.models import LoginRequest, User

    create_all()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            err_line(f"user '{email}' not found")
            raise typer.Exit(1)
        if user.is_disabled:
            err_line(f"user '{email}' is disabled")
            raise typer.Exit(1)
        existing = (
            db.query(LoginRequest)
            .filter(LoginRequest.user_id == user.id, LoginRequest.status == "pending")
            .first()
        )
        if existing:
            console.print(f"[{WARN}]\u25cf pending request already exists for {email}[/{WARN}]")
            return
        req = LoginRequest(
            user_id=user.id,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )
        db.add(req)
        db.commit()
        console.print(f"{GL_OK} [{OK}]created login request[/{OK}]  {email}")
    finally:
        db.close()


@cockpit_request_app.command("approve")
def cockpit_request_approve(
    email: str = typer.Argument(...),
):
    """Approve a pending login request and mint a magic link token."""
    from kai.cockpit.auth_backends import MagicLinkProvider
    from kai.cockpit.cli_helpers import build_magic_link_url
    from kai.cockpit.db import SessionLocal, create_all
    from kai.cockpit.mailer import MailError, send_magic_link
    from kai.cockpit.models import User

    create_all()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            err_line(f"user '{email}' not found")
            raise typer.Exit(1)

        # Mint via the shared AuthProvider seam so a single mint path handles
        # the token logic. Requires a pending LoginRequest to exist.
        provider = MagicLinkProvider(db)
        try:
            token = provider.initiate_login(user.id)
        except ValueError as exc:
            err_line(str(exc))
            raise typer.Exit(1) from exc

        magic_url = build_magic_link_url(token.token)
        try:
            send_magic_link(email, magic_url)
        except MailError as exc:
            err_line(str(exc))
            raise typer.Exit(1) from exc
        console.print(f"{GL_OK} [{OK}]token minted[/{OK}]  {email}")
    finally:
        db.close()
