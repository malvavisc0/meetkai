"""CLI tests for `kai deployment ...` commands."""

from typer.testing import CliRunner

from kai.cli import app

runner = CliRunner()


def _create_user(email: str = "bob@x.com") -> None:
    runner.invoke(
        app,
        ["cockpit", "user", "create", email, "--language", "English", "--timezone", "UTC"],
    )


def _connect_whatsapp(email: str = "bob@x.com") -> None:
    """Insert a connected WhatsApp ``Connection`` row directly — bypasses
    ``connection connect``, which talks to a real WAHA instance and isn't
    mockable at the CLI-invocation boundary these tests use.
    ``DeploymentsService.create()`` requires this connection to exist.
    """
    from kai.cockpit.db import SessionLocal
    from kai.cockpit.models import Connection, User

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.email == email).first()
        assert db_user is not None, f"user '{email}' not found"
        db.add(
            Connection(
                user_id=db_user.id,
                service="whatsapp",
                status="connected",
                config={
                    "waha_session": "kai-bob",
                    "waha_webhook_port": 8101,
                    "waha_webhook_path": "/webhook/whatsapp-1",
                },
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
    finally:
        db.close()


def _create_deployment(email: str = "bob@x.com") -> int:
    _connect_whatsapp(email)
    res = runner.invoke(
        app,
        [
            "deployment",
            "create",
            "--user",
            email,
            "--bot",
            "waha",
            "--goal",
            "be helpful",
            "--language",
            "English",
        ],
    )
    assert res.exit_code == 0, res.stdout
    # id=N appears in stdout from the create command
    import re

    m = re.search(r"id=(\d+)", res.stdout)
    assert m, f"could not parse deployment id from: {res.stdout!r}"
    return int(m.group(1))


class TestDeploymentDelete:
    def test_delete_requires_existing(self):
        _create_user()
        result = runner.invoke(app, ["deployment", "delete", "999", "--yes"])
        assert result.exit_code != 0
        assert "not found" in result.stdout

    def test_delete_with_yes_skips_prompt(self):
        _create_user()
        dep_id = _create_deployment()
        result = runner.invoke(app, ["deployment", "delete", str(dep_id), "--yes"])
        assert result.exit_code == 0
        assert "deleted deployment" in result.stdout

        listing = runner.invoke(app, ["deployment", "list", "--user", "bob@x.com"])
        assert "no deployments" in listing.stdout

    def test_delete_without_yes_prompts_and_aborts(self):
        _create_user()
        dep_id = _create_deployment()
        # CliRunner answers "n" to confirm() by default when input is None?
        # Supply stdin "n\n" to decline.
        result = runner.invoke(app, ["deployment", "delete", str(dep_id)], input="n\n")
        assert result.exit_code == 0
        assert "aborted" in result.stdout
        # still listed
        listing = runner.invoke(app, ["deployment", "list", "--user", "bob@x.com"])
        assert str(dep_id) in listing.stdout
