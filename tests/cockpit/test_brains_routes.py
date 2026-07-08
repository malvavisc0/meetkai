"""Tests for the /brain cockpit routes (create + instruction form)."""

import io
from unittest.mock import AsyncMock

import pytest

from kai.brain.client import DocumentRecord, IngestResult
from kai.brain.crawler import CrawlLinks, CrawlPage, MarkdownResult
from kai.cockpit import tokens
from kai.cockpit.auth_backends import MagicLinkProvider
from kai.cockpit.models import User
from kai.cockpit.naming import kai_slug_for


@pytest.fixture
def fake_lightrag_client(monkeypatch):
    client = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr("kai.cockpit.brains.BrainsService._lightrag_client", lambda self: client)
    return client


@pytest.fixture
def fake_crawler_client(monkeypatch):
    client = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr("kai.cockpit.brains.BrainsService._crawler_client", lambda self: client)
    return client


@pytest.fixture
def bob(db):
    u = User(
        email="bob@x.com",
        language="Spanish",
        timezone="Europe/Berlin",
        hmac_key="bob-hmac-key",
        created_at="now",
        is_disabled=False,
        kai_slug=kai_slug_for("bob@x.com"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _login(client, db, bob):
    tokens.create_login_request(db, bob.id)
    provider = MagicLinkProvider(db)
    token = provider.initiate_login(bob.id)
    resp = client.get(f"/login/auth?token={token.token}", follow_redirects=False)
    assert resp.status_code == 302
    return client


class TestBrainsPage:
    def test_requires_login(self, client):
        r = client.get("/brain", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_empty_state_before_creation(self, client, db, bob):
        _login(client, db, bob)
        r = client.get("/brain")
        assert r.status_code == 200
        assert "Create Brain" in r.text

    def test_shows_brain_after_creation(self, client, db, bob):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        r = client.get("/brain")
        assert r.status_code == 200
        assert "READY" in r.text  # brain exists → status summary badge


class TestBrainsCreate:
    def test_creates_row(self, client, db, bob):
        from kai.cockpit.brains import BrainsService

        _login(client, db, bob)
        assert BrainsService(db).get_brain(bob) is None
        resp = client.post("/brain/create", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/brain"
        brain = BrainsService(db).get_brain(bob)
        assert brain is not None
        assert brain.config["workspace"] == "kai-v001-bob_at_x_com"

    def test_idempotent_via_route(self, client, db, bob):
        from kai.cockpit.brains import BrainsService

        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        client.post("/brain/create", follow_redirects=False)
        assert (
            db.query(BrainsService(db).get_brain(bob).__class__)
            .filter_by(service="lightrag", user_id=bob.id)
            .count()
            == 1
        )


class TestBrainsInstruction:
    def test_requires_existing_brain(self, client, db, bob):
        _login(client, db, bob)
        resp = client.post(
            "/brain/instruction",
            data={"instruction": "ask about pricing", "mandatory": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        r = client.get("/brain")
        assert "Create Brain" in r.text  # still not created

    def test_saves_instruction(self, client, db, bob):
        from kai.cockpit.brains import BrainsService

        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        resp = client.post(
            "/brain/instruction",
            data={"instruction": "how to do X from section Y", "mandatory": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        brain = BrainsService(db).get_brain(bob)
        assert brain is not None
        assert brain.config["instruction"] == "how to do X from section Y"
        assert brain.config["mandatory"] is True

        r = client.get("/brain")
        assert "how to do X from section Y" in r.text

    def test_unchecked_checkbox_clears_mandatory(self, client, db, bob):
        from kai.cockpit.brains import BrainsService

        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        client.post(
            "/brain/instruction",
            data={"instruction": "x", "mandatory": "true"},
            follow_redirects=False,
        )
        # Unchecked checkboxes are simply absent from form data.
        client.post(
            "/brain/instruction",
            data={"instruction": "x"},
            follow_redirects=False,
        )
        brain = BrainsService(db).get_brain(bob)
        assert brain is not None
        assert brain.config["mandatory"] is False


class TestBrainsUpload:
    def test_uploads_file_and_flashes_success(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        fake_lightrag_client.ingest_file.return_value = IngestResult(
            track_id="upload_1", status="success", message="ok"
        )

        resp = client.post(
            "/brain/upload",
            files={"file": ("handbook.pdf", io.BytesIO(b"hello world"), "application/pdf")},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        fake_lightrag_client.ingest_file.assert_awaited_once()
        _, kwargs = fake_lightrag_client.ingest_file.await_args
        assert kwargs["filename"] == "handbook.pdf"
        assert kwargs["workspace"] == "kai-v001-bob_at_x_com"

        r = client.get("/brain")
        assert "Uploaded handbook.pdf" in r.text

    def test_fails_gracefully_without_a_brain(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        resp = client.post(
            "/brain/upload",
            files={"file": ("notes.txt", io.BytesIO(b"hi"), "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        r = client.get("/brain")
        assert "No Brain provisioned for this Operator" in r.text


class TestBrainsIngestUrl:
    @staticmethod
    def _page(url: str, markdown: str, internal: list[str] | None = None) -> CrawlPage:
        return CrawlPage(
            url=url,
            success=True,
            markdown=MarkdownResult(raw_markdown=markdown),
            links=CrawlLinks(internal=internal or []),
        )

    def test_fetches_and_ingests(self, client, db, bob, fake_lightrag_client, fake_crawler_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)

        def _crawl(*, url: str) -> CrawlPage:
            if url == "https://example.com/docs":
                return self._page(url, "# Docs\ncontent", ["https://example.com/download"])
            return self._page(url, f"# {url}")

        fake_crawler_client.crawl = AsyncMock(side_effect=_crawl)
        fake_lightrag_client.ingest_texts.return_value = IngestResult(
            track_id="insert_1", status="success", message="ok"
        )

        resp = client.post(
            "/brain/ingest-url",
            data={"url": "https://example.com/docs"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert fake_crawler_client.crawl.await_count == 2  # seed + 1 linked page
        fake_lightrag_client.ingest_texts.assert_awaited_once()
        _, kwargs = fake_lightrag_client.ingest_texts.await_args
        assert kwargs["workspace"] == "kai-v001-bob_at_x_com"
        assert kwargs["file_sources"] == ["example-com-docs", "example-com-download"]

        r = client.get("/brain")
        assert "Added https://example.com/docs" in r.text
        assert "2 pages" in r.text

    def test_empty_url_rejected(self, client, db, bob, fake_lightrag_client, fake_crawler_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        resp = client.post("/brain/ingest-url", data={"url": "   "}, follow_redirects=False)
        assert resp.status_code == 302
        fake_crawler_client.crawl.assert_not_awaited()


class TestBrainsIngestText:
    def test_adds_text(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        fake_lightrag_client.ingest_text.return_value = IngestResult(
            track_id="insert_1", status="success", message="ok"
        )

        resp = client.post(
            "/brain/ingest-text",
            data={"name": "Onboarding notes", "text": "Refund: 30 days."},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        fake_lightrag_client.ingest_text.assert_awaited_once_with(
            file_source="Onboarding notes",
            text="Refund: 30 days.",
            workspace="kai-v001-bob_at_x_com",
        )

        r = client.get("/brain")
        assert "Added Onboarding notes" in r.text

    def test_empty_text_rejected(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        resp = client.post(
            "/brain/ingest-text",
            data={"name": "notes", "text": "   "},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        fake_lightrag_client.ingest_text.assert_not_awaited()


class TestBrainsDocumentsList:
    def test_lists_documents_from_lightrag(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        fake_lightrag_client.list_docs.return_value = [
            DocumentRecord(
                id="doc-1",
                track_id="insert_1",
                status="processed",
                file_path="handbook.pdf",
                chunks_count=3,
                content_length=100,
                created_at="t1",
                updated_at="t2",
            )
        ]

        r = client.get("/brain")
        assert "handbook.pdf" in r.text
        assert "READY" in r.text  # terminal doc renders the ready badge, not raw status

    def test_shows_error_when_lightrag_unreachable(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        fake_lightrag_client.list_docs.side_effect = RuntimeError("connection refused")

        r = client.get("/brain")
        assert "Could not load documents" in r.text

    def test_adds_meta_refresh_while_a_doc_is_processing(
        self, client, db, bob, fake_lightrag_client
    ):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        fake_lightrag_client.list_docs.return_value = [
            DocumentRecord(
                id="doc-1",
                track_id="insert_1",
                status="processing",
                file_path="handbook.pdf",
                chunks_count=None,
                content_length=100,
                created_at="t1",
                updated_at="t2",
            )
        ]

        r = client.get("/brain")
        assert '<meta http-equiv="refresh"' in r.text


class TestBrainsDeleteDocument:
    def test_deletes_document(self, client, db, bob, fake_lightrag_client):
        _login(client, db, bob)
        client.post("/brain/create", follow_redirects=False)
        fake_lightrag_client.delete_doc.return_value = "deletion_started"

        resp = client.post(
            "/brain/documents/delete", data={"doc_id": "doc-1"}, follow_redirects=False
        )
        assert resp.status_code == 302
        fake_lightrag_client.delete_doc.assert_awaited_once_with(
            doc_id="doc-1", workspace="kai-v001-bob_at_x_com"
        )

        r = client.get("/brain")
        assert "Document deleted" in r.text
