"""Shared cockpit test helpers — importable without relying on conftest discovery.

Lives outside ``conftest.py`` so test modules can import it directly via
``from tests.cockpit.helpers import _connect_whatsapp`` regardless of
pytest collection order. Keeping it a plain module (not a fixture) means
the empty ``tests/__init__.py`` / ``tests/cockpit/__init__.py`` files —
which previously existed only to make that import resolvable — are no
longer needed and have been removed. Removing the ``__init__.py`` files
restores pytest's default rootdir-based conftest discovery, which the
package-marker version was breaking for ``test_db_models.py`` in mixed
multi-directory runs.
"""

from datetime import UTC, datetime


def _connect_whatsapp(db, user, status: str = "connected"):
    """Create (or update) a WhatsApp ``Connection`` row for ``user``.

    Plain helper, not a fixture, so tests that manage their own Connection
    rows (or specifically test the disconnected/missing-connection path)
    are never forced through it.
    """
    from kai.cockpit.models import Connection

    conn = Connection(
        user_id=user.id,
        service="whatsapp",
        status=status,
        config={
            "waha_session": f"kai-{user.kai_slug}",
            "waha_webhook_port": 8101,
            "waha_webhook_path": "/webhook/whatsapp-1",
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
# same token without re-hitting ``GET /_csrf``. Keyed by ``id(client)`` so
# distinct TestClient instances (each with its own cookie jar) stay separate.
_cached_csrf_token: dict[int, str] = {}


def _fetch_csrf_token(client) -> str:
    """Fetch (and cache) the session-scoped CSRF token via the test-only
    ``GET /_csrf`` endpoint registered when the cockpit is in testing mode.

    The endpoint is only mounted while ``KAI_COCKPIT_TESTING=1`` (see
    conftest's ``_cockpit_env``), so this helper is safe to call from any
    cockpit test but meaningless for the bot webhook tests that build their
    own Starlette app. Those tests go through ``client.post`` directly and
    need no CSRF token (webhook ingress is exempt by middleware policy).
    """
    cached = _cached_csrf_token.get(id(client))
    if cached:
        return cached
    resp = client.get("/_csrf")
    if resp.status_code != 200:
        raise RuntimeError(
            f"CSRF token endpoint returned {resp.status_code}; "
            "ensure the cockpit app is in testing mode (KAI_COCKPIT_TESTING=1)"
        )
    token = resp.json()["token"]
    _cached_csrf_token[id(client)] = token
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
