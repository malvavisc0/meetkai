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
            "waha_session": f"kai-{user.kai_slug or user.email.split('@')[0]}",
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
