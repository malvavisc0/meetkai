"""CLI tests for `kai cockpit user ...` / `kai cockpit request ...`."""

from typer.testing import CliRunner

from kai.cli import app

runner = CliRunner()


class TestUserCreate:
    def test_create_requires_language_and_timezone(self):
        result = runner.invoke(app, ["cockpit", "user", "create", "bob@x.com"])
        assert result.exit_code != 0

    def test_create_happy_path(self):
        result = runner.invoke(
            app,
            [
                "cockpit",
                "user",
                "create",
                "bob@x.com",
                "--language",
                "English",
                "--timezone",
                "UTC",
            ],
        )
        assert result.exit_code == 0
        assert "bob@x.com" in result.stdout

    def test_create_duplicate_fails(self):
        args = [
            "cockpit",
            "user",
            "create",
            "bob@x.com",
            "--language",
            "English",
            "--timezone",
            "UTC",
        ]
        assert runner.invoke(app, args).exit_code == 0
        result = runner.invoke(app, args)
        assert result.exit_code != 0
        assert "already exists" in result.stdout

    def test_create_persists_kai_slug(self, db):
        result = runner.invoke(
            app,
            [
                "cockpit",
                "user",
                "create",
                "carol@example.net",
                "--language",
                "English",
                "--timezone",
                "UTC",
            ],
        )
        assert result.exit_code == 0

        from kai.cockpit.models import User

        user = db.query(User).filter(User.email == "carol@example.net").first()
        assert user is not None
        # Same slug reused verbatim for both the WAHA session name
        # (ConnectionsService) and the LightRAG workspace name (BrainsService).
        assert user.kai_slug == "kai-v001-carol_at_example_net"


class TestUserList:
    def test_list_empty(self):
        result = runner.invoke(app, ["cockpit", "user", "list"])
        assert result.exit_code == 0
        assert "no users found" in result.stdout

    def test_list_shows_created_users(self):
        runner.invoke(
            app,
            [
                "cockpit",
                "user",
                "create",
                "bob@x.com",
                "--language",
                "English",
                "--timezone",
                "UTC",
            ],
        )
        result = runner.invoke(app, ["cockpit", "user", "list"])
        assert result.exit_code == 0
        assert "bob@x.com" in result.stdout


class TestUserDisable:
    def test_disable_unknown_user_fails(self):
        result = runner.invoke(app, ["cockpit", "user", "disable", "nobody@x.com"])
        assert result.exit_code != 0

    def test_disable_happy_path(self):
        runner.invoke(
            app,
            [
                "cockpit",
                "user",
                "create",
                "bob@x.com",
                "--language",
                "English",
                "--timezone",
                "UTC",
            ],
        )
        result = runner.invoke(app, ["cockpit", "user", "disable", "bob@x.com"])
        assert result.exit_code == 0

        listing = runner.invoke(app, ["cockpit", "user", "list"])
        # is_disabled column should now say "yes" for bob
        assert "yes" in listing.stdout


class TestLoginRequestFlow:
    def _create_user(self):
        runner.invoke(
            app,
            [
                "cockpit",
                "user",
                "create",
                "bob@x.com",
                "--language",
                "English",
                "--timezone",
                "UTC",
            ],
        )

    def test_request_create_requires_existing_user(self):
        result = runner.invoke(app, ["cockpit", "request", "create", "nobody@x.com"])
        assert result.exit_code != 0

    def test_request_create_and_list(self):
        self._create_user()
        result = runner.invoke(app, ["cockpit", "request", "create", "bob@x.com"])
        assert result.exit_code == 0

        listing = runner.invoke(app, ["cockpit", "request", "list"])
        assert listing.exit_code == 0
        assert "bob@x.com" in listing.stdout

    def test_request_create_disabled_user_fails(self):
        self._create_user()
        runner.invoke(app, ["cockpit", "user", "disable", "bob@x.com"])
        result = runner.invoke(app, ["cockpit", "request", "create", "bob@x.com"])
        assert result.exit_code != 0

    def test_request_approve_mints_token(self, monkeypatch, capsys):
        self._create_user()
        runner.invoke(app, ["cockpit", "request", "create", "bob@x.com"])

        # Force the print-to-stdout mailer path (SMTP disabled).
        monkeypatch.setenv("KAI_SMTP_HOST", "")

        result = runner.invoke(app, ["cockpit", "request", "approve", "bob@x.com"])
        assert result.exit_code == 0
        assert "token minted" in result.stdout

    def test_request_approve_without_pending_fails(self):
        self._create_user()
        result = runner.invoke(app, ["cockpit", "request", "approve", "bob@x.com"])
        assert result.exit_code != 0
