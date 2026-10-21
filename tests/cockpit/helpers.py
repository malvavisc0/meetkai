"""Shared cockpit test helpers — importable without relying on conftest discovery.

Lives outside ``conftest.py`` so test modules can import it directly via
``from tests.cockpit.helpers import _connect_email`` regardless of pytest
collection order.
"""

import weakref
from datetime import UTC, datetime
from typing import Any


def make_fake_proc(
    run_id: str = "deadbeef",
    *,
    lines: list[str] | None = None,
    returncode: int | None = None,
):
    """Return a Popen stand-in that streams a KAI_RUN_ID line on stdout."""
    startup_lines = lines if lines is not None else ["starting...\n", f"KAI_RUN_ID={run_id}\n"]

    class FakeProc:
        def __init__(self) -> None:
            self._lines = iter(startup_lines)
            self.returncode = returncode

        @property
        def stdout(self):
            return self

        def readline(self) -> str:
            return next(self._lines, "")

        def poll(self):
            return self.returncode

    return FakeProc()


def seed_run_registry(
    tmp_path,
    instance_id: str,
    *,
    run_id: str = "deadbeef",
    endpoint: str = "http://x",
    pid_start: int | None = None,
):
    """Seed a run registry entry and return ``(settings, registry)``.

    ``pid_start`` defaults to the current process's start tick (a live-looking
    record); pass ``1`` for a PID-reused/dead record.
    """
    import os

    from kai.config.settings import Settings
    from kai.runs import RunRecord, RunRegistry, proc_start_time, runs_path

    settings = Settings.for_test(agent_history_folder=str(tmp_path))
    registry = RunRegistry(runs_path(settings.agent_history_folder, instance_id))
    record = RunRecord(
        run_id=run_id,
        endpoint=endpoint,
        hmac_key="k",
        hmac_algorithm="sha512",
        pid=os.getpid(),
        pid_start=proc_start_time(os.getpid()) if pid_start is None else pid_start,
        started_at="t",
    )
    registry.replace(record)
    return settings, registry


def _connect_email(db, user, status: str = "connected"):
    """Create (or update) an email/Resend ``Connection`` row for ``user``.

    Plain helper, not a fixture, so tests that manage their own Connection
    rows (or specifically test the disconnected/missing-connection path)
    are never forced through it.
    """
    from kai.cockpit.models import Connection

    conn = Connection(
        user_id=user.id,
        service="resend",
        status=status,
        config={"signing_secret": "test-signing", "api_key": "test-api-key"},
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def _connect_smtp(db, user, status: str = "connected"):
    """Create (or update) a plaintext SMTP ``Connection`` row for ``user``.

    The email bot requires ``resend`` + ``smtp`` — this is the SMTP half.
    Plain helper, not a fixture, for the same reason as ``_connect_email``.
    """
    from kai.cockpit.models import Connection

    conn = Connection(
        user_id=user.id,
        service="smtp",
        status=status,
        config={
            "host": "smtp.example.com",
            "port": 587,
            "username": "u",
            "password": "p",
            "from_address": "a@b.c",
            "use_tls": True,
        },
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


# --- CSRF (Workstream A1) -----------------------------------------------------
#
# Module-level cache so repeated ``csrf_post`` calls in one test share the
# same token without re-hitting ``GET /_csrf``. Keyed weakly by the client
# object so distinct TestClient instances (each with its own cookie jar)
# stay separate. A plain ``id(client)`` key is unsafe: under pytest-xdist a
# client is released after its test and a later client can be allocated at
# the same address, making the lookup return a dead client's token.
_cached_csrf_token: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def _fetch_csrf_token(client) -> str:
    """Fetch (and cache) the session-scoped CSRF token via the test-only
    ``GET /_csrf`` endpoint registered when the cockpit is in testing mode.

    The endpoint is only mounted while ``KAI_COCKPIT_TESTING=1`` (see
    conftest's ``_cockpit_env``), so this helper is safe to call from any
    cockpit test but meaningless for the bot webhook tests that build their
    own Starlette app. Those tests go through ``client.post`` directly and
    need no CSRF token (webhook ingress is exempt by middleware policy).
    """
    cached = _cached_csrf_token.get(client)
    if cached:
        return cached
    resp = client.get("/_csrf")
    if resp.status_code != 200:
        raise RuntimeError(
            f"CSRF token endpoint returned {resp.status_code}; "
            "ensure the cockpit app is in testing mode (KAI_COCKPIT_TESTING=1)"
        )
    token = resp.json()["token"]
    _cached_csrf_token[client] = token
    return token


def csrf_post(client, url, **kwargs):
    """Drop-in replacement for ``client.post`` that attaches a CSRF token.

    Mirrors the server-side middleware's two accepted token locations:

    * Form POSTs (with or without ``data`` / ``files``): merge ``_csrf``
      into the form data dict, preserving any caller-supplied keys.
    * JSON POSTs (``json=...``): the body is opaque, so send the token via
      the ``X-CSRF-Token`` header instead.

    The token is fetched lazily from ``GET /_csrf`` and cached on the
    client instance, so a single round-trip suffices per test.

    Webhook (``/webhook/...``) and the bare ``/api/escalations`` ingest
    endpoint are machine-consumer routes — the middleware exempts them, so
    tests for those paths should keep using ``client.post`` directly.
    """
    token = _fetch_csrf_token(client)
    if "json" in kwargs and kwargs["json"] is not None:
        headers = dict(kwargs.get("headers") or {})
        headers["X-CSRF-Token"] = token
        kwargs["headers"] = headers
    else:
        data = dict(kwargs.get("data") or {})
        data["_csrf"] = token
        kwargs["data"] = data
    return client.post(url, **kwargs)
