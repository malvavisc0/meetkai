"""ORM models for the cockpit database.

SQLAlchemy 2.0 declarative (Mapped / mapped_column). No relationships in v1
— all joins are explicit via foreign keys.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from kai.cockpit.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    is_disabled: Mapped[bool] = mapped_column(default=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    hmac_key: Mapped[str] = mapped_column(String, nullable=False)
    # Admin-granted entitlement flags (image, video, stt, tts, sso, ...).
    # A deployment may only enable a flag that the user is entitled to —
    # the settings form enforces this server-side so a direct POST cannot
    # bypass it. Defaults to empty (all off) on user creation.
    feature_flags: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    # Stable external-service identifier generated once at user creation
    # (kai.cockpit.naming.kai_slug_for) and reused verbatim as both the
    # WAHA session name and the LightRAG workspace name — never
    # recomputed. Nullable to allow lazy backfilling of rows that predate
    # this column (see scripts/backfill_kai_slug.py).
    kai_slug: Mapped[str | None] = mapped_column(String, nullable=True)


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("user_id", "bot_type"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    bot_type: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="needs_connect")
    desired_state: Mapped[str] = mapped_column(String, nullable=False, default="stopped")
    voice: Mapped[str] = mapped_column(String, nullable=False)
    goal: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    # Template selected for this deployment. ``"general"`` is the always-safe
    # default (resolves without --template on the CLI).
    template: Mapped[str] = mapped_column(String, nullable=False, default="general")
    # Operator tool overrides from the cockpit settings form, shape:
    # ``{"enable": [...], "disable": [...]}``. Persisted verbatim and passed to
    # ``resolve_tools()`` at spawn.
    tool_overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    feature_flags: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Per-deployment Brain overrides, both nullable. `brain_mandatory=None`
    # behaves the same as `False` (Brain available, not forced).
    # `brain_instruction=None` means: use the Brain connection's own
    # instruction text instead of a per-deployment override. Actually
    # applied in DeploymentsService.start(), not here.
    brain_mandatory: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    brain_instruction: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # True when settings were edited while the bot was running, so the live
    # process has stale config in memory and a restart is needed to apply
    # the on-disk config. Set in DeploymentsService.edit() when running,
    # cleared in start()/stop(). Persists across reloads/sessions (unlike
    # the prior session-flash signal, which was lost on reload).
    needs_restart: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("user_id", "service"),
        # Enforces exclusive port allocation at the DB level (SQLite/most
        # backends allow multiple NULLs through a unique constraint, so
        # non-whatsapp connections that never set this column are unaffected).
        UniqueConstraint("webhook_port"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    service: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="disconnected")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Mirrors config["waha_webhook_port"] as a real column so the DB can
    # enforce exclusive allocation (see get_or_create_whatsapp). NULL for
    # connections that don't allocate a port.
    # provider-facing bespoke-transport port (WhatsApp/WAHA only); DO NOT
    # populate for ingress-only or other connection types — this constraint
    # is table-wide, not per-service.
    webhook_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class LoginRequest(Base):
    __tablename__ = "login_requests"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    fulfilled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    token_id: Mapped[str | None] = mapped_column(String, nullable=True)


class LoginToken(Base):
    __tablename__ = "login_tokens"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    consumed_at: Mapped[str | None] = mapped_column(String, nullable=True)
