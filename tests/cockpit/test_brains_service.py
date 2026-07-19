"""Tests for kai.cockpit.brains.BrainsService."""

import io
from unittest.mock import AsyncMock

import pytest

from kai.brain.client import DocumentRecord, IngestResult
from kai.brain.crawler import CrawlLinks, CrawlPage, MarkdownResult
from kai.cockpit.brains import BrainsService, _slug_for_url
from kai.cockpit.models import Connection


class TestCreateBrain:
    def test_creates_row_with_workspace(self, db, user):
        svc = BrainsService(db)
        assert svc.get_brain(user) is None
        brain = svc.create_brain(user)
        assert brain.service == "morphik"
        assert brain.status == "ready"
        # bob@test.com -> kai-v001-bob_at_test_com (same scheme as WAHA)
        assert brain.config["workspace"] == "kai-v001-bob_at_test_com"
        assert brain.config["instruction"] == ""
        assert "mandatory" not in brain.config

    def test_idempotent(self, db, user):
        svc = BrainsService(db)
        first = svc.create_brain(user)
        second = svc.create_brain(user)
        assert first.id == second.id
        # workspace doesn't get regenerated/changed on a second call
        assert first.config["workspace"] == second.config["workspace"]

    def test_no_row_before_creation(self, db, user):
        svc = BrainsService(db)
        assert svc.get_brain(user) is None


class TestUpdateInstruction:
    def test_raises_without_a_brain(self, db, user):
        svc = BrainsService(db)
        with pytest.raises(ValueError):
            svc.update_instruction(user, instruction="ask about pricing")

    def test_saves_instruction(self, db, user):
        svc = BrainsService(db)
        svc.create_brain(user)
        updated = svc.update_instruction(user, instruction="how to do X from section Y")
        assert updated.config["instruction"] == "how to do X from section Y"
        # workspace untouched by an instruction update
        assert updated.config["workspace"] == "kai-v001-bob_at_test_com"

    def test_update_persists_across_fresh_query(self, db, user):
        svc = BrainsService(db)
        svc.create_brain(user)
        svc.update_instruction(user, instruction="refund policy")
        reloaded = svc.get_brain(user)
        assert reloaded is not None
        assert reloaded.config["instruction"] == "refund policy"


class TestDeleteBrain:
    def test_removes_row(self, db, user):
        svc = BrainsService(db)
        svc.create_brain(user)
        svc.delete_brain(user)
        assert svc.get_brain(user) is None
        assert db.query(Connection).filter(Connection.service == "morphik").count() == 0

    def test_noop_when_no_brain(self, db, user):
        svc = BrainsService(db)
        svc.delete_brain(user)  # must not raise
        assert svc.get_brain(user) is None


class TestSlugForUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://transmissionbt.com/download", "transmissionbt-com-download"),
            ("https://example.com/docs/", "example-com-docs"),
            ("https://example.com/", "example-com"),
        ],
    )
    def test_derives_readable_slug(self, url, expected):
        assert _slug_for_url(url) == expected


@pytest.fixture
def fake_morphik_client(monkeypatch):
    """Patch BrainsService._morphik_client with an AsyncMock MorphikClient."""
    client = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr("kai.cockpit.brains.BrainsService._morphik_client", lambda self: client)
    return client


@pytest.fixture
def fake_crawler_client(monkeypatch):
    """Patch BrainsService._crawler_client with an AsyncMock Crawl4aiClient."""
    client = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr("kai.cockpit.brains.BrainsService._crawler_client", lambda self: client)
    return client


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Stub out real DNS lookups in ``validate_ingest_url`` for these tests.

    Every hostname resolves to a public IP unless explicitly listed as
    private/internal, so tests don't depend on live network access and
    negative (SSRF) cases can be exercised deterministically.
    """
    private_hosts = {
        "localhost": ["127.0.0.1"],
        "metadata.internal": ["169.254.169.254"],
        "intranet.local": ["10.0.0.5"],
    }

    def _resolve(host: str) -> list[str]:
        return private_hosts.get(host, ["93.184.216.34"])

    monkeypatch.setattr("kai.brain.validation._resolve_all", _resolve)


class TestIngestText:
    async def test_raises_without_a_brain(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        with pytest.raises(ValueError):
            await svc.ingest_text(user, name="notes", text="hello")

    async def test_calls_client_with_workspace_and_closes(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_morphik_client.ingest_text.return_value = IngestResult(
            track_id="insert_1", status="success", message="ok"
        )
        result = await svc.ingest_text(user, name="Onboarding notes", text="Refund: 30 days.")
        assert result.track_id == "insert_1"
        fake_morphik_client.ingest_text.assert_awaited_once_with(
            file_source="Onboarding notes",
            text="Refund: 30 days.",
            workspace="kai-v001-bob_at_test_com",
        )
        fake_morphik_client.close.assert_awaited_once()


class TestIngestFile:
    async def test_calls_client_with_filename_and_workspace(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_morphik_client.ingest_file.return_value = IngestResult(
            track_id="upload_1", status="success", message="ok"
        )
        fake_file = io.BytesIO(b"dummy")
        result = await svc.ingest_file(user, filename="handbook.pdf", file=fake_file)
        assert result.track_id == "upload_1"
        fake_morphik_client.ingest_file.assert_awaited_once_with(
            file=fake_file, filename="handbook.pdf", workspace="kai-v001-bob_at_test_com"
        )
        fake_morphik_client.close.assert_awaited_once()

    async def test_rejects_disallowed_extension(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_file = io.BytesIO(b"dummy")
        with pytest.raises(ValueError, match="Unsupported file type"):
            await svc.ingest_file(user, filename="malware.exe", file=fake_file)
        fake_morphik_client.ingest_file.assert_not_awaited()

    async def test_rejects_oversized_file(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_file = io.BytesIO(b"x" * (26 * 1024 * 1024))
        with pytest.raises(ValueError, match="too large"):
            await svc.ingest_file(user, filename="big.txt", file=fake_file)
        fake_morphik_client.ingest_file.assert_not_awaited()

    async def test_rejects_empty_file(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_file = io.BytesIO(b"")
        with pytest.raises(ValueError, match="empty"):
            await svc.ingest_file(user, filename="empty.txt", file=fake_file)
        fake_morphik_client.ingest_file.assert_not_awaited()


class TestIngestUrl:
    @staticmethod
    def _page(url: str, markdown: str, internal: list[str] | None = None) -> CrawlPage:
        return CrawlPage(
            url=url,
            success=True,
            markdown=MarkdownResult(raw_markdown=markdown),
            links=CrawlLinks(internal=internal or []),
        )

    async def test_crawls_seed_plus_linked_pages_then_batch_ingests(
        self, db, user, fake_morphik_client, fake_crawler_client
    ):
        svc = BrainsService(db)
        svc.create_brain(user)

        def _crawl(*, url: str) -> CrawlPage:
            if url == "https://example.com/docs":
                return self._page(
                    url,
                    "# Docs\nSome content.",
                    ["https://example.com/download", "https://example.com/addons"],
                )
            return self._page(url, f"# {url}\nbody")

        fake_crawler_client.crawl = AsyncMock(side_effect=_crawl)
        fake_morphik_client.ingest_texts.return_value = IngestResult(
            track_id="insert_2", status="success", message="ok"
        )

        result = await svc.ingest_url(user, url="https://example.com/docs")

        assert result.track_id == "insert_2"
        # depth 1 (default): seed + 2 linked pages = 3 fetches
        assert fake_crawler_client.crawl.await_count == 3
        fake_crawler_client.close.assert_awaited_once()
        fake_morphik_client.ingest_texts.assert_awaited_once()
        _, kwargs = fake_morphik_client.ingest_texts.await_args
        assert kwargs["workspace"] == "kai-v001-bob_at_test_com"
        assert kwargs["file_sources"] == [
            "example-com-docs",
            "example-com-download",
            "example-com-addons",
        ]
        assert len(kwargs["texts"]) == 3
        assert kwargs["texts"][0] == "# Docs\nSome content."
        fake_morphik_client.close.assert_awaited_once()

    async def test_does_not_follow_external_or_non_http_links(
        self, db, user, fake_morphik_client, fake_crawler_client
    ):
        svc = BrainsService(db)
        svc.create_brain(user)

        def _crawl(*, url: str) -> CrawlPage:
            return self._page(
                url,
                f"# {url}",
                [
                    "https://example.com/about",
                    "https://other.example/external",
                    "mailto:hello@example.com",
                    "/relative-page",
                ],
            )

        fake_crawler_client.crawl = AsyncMock(side_effect=_crawl)
        fake_morphik_client.ingest_texts.return_value = IngestResult(
            track_id="t", status="success", message="ok"
        )

        await svc.ingest_url(user, url="https://example.com/")

        fetched = {call.kwargs["url"] for call in fake_crawler_client.crawl.await_args_list}
        assert "https://example.com/" in fetched
        assert "https://example.com/about" in fetched
        assert "https://example.com/relative-page" in fetched
        # external host + mailto never fetched
        assert "https://other.example/external" not in fetched
        assert all(not u.startswith("mailto:") for u in fetched)

    async def test_raises_when_no_content_crawled(
        self, db, user, fake_morphik_client, fake_crawler_client
    ):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_crawler_client.crawl = AsyncMock(
            return_value=CrawlPage(
                url="https://example.com/empty",
                success=False,
                markdown=MarkdownResult(),
                links=CrawlLinks(),
                error_message="Navigation timeout",
            )
        )

        with pytest.raises(ValueError):
            await svc.ingest_url(user, url="https://example.com/empty")

        fake_morphik_client.ingest_texts.assert_not_awaited()

    async def test_raises_without_a_brain(self, db, user, fake_crawler_client):
        svc = BrainsService(db)
        with pytest.raises(ValueError):
            await svc.ingest_url(user, url="https://example.com/docs")
        fake_crawler_client.crawl.assert_not_awaited()

    async def test_rejects_non_http_scheme(self, db, user, fake_crawler_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        with pytest.raises(ValueError, match="http"):
            await svc.ingest_url(user, url="ftp://example.com/")
        fake_crawler_client.crawl.assert_not_awaited()

    async def test_rejects_loopback_host(self, db, user, fake_crawler_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        with pytest.raises(ValueError, match="private, local, or reserved"):
            await svc.ingest_url(user, url="http://localhost/")
        fake_crawler_client.crawl.assert_not_awaited()

    async def test_rejects_cloud_metadata_host(self, db, user, fake_crawler_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        with pytest.raises(ValueError, match="private, local, or reserved"):
            await svc.ingest_url(user, url="http://metadata.internal/latest/meta-data/")
        fake_crawler_client.crawl.assert_not_awaited()

    async def test_rejects_private_ip_host(self, db, user, fake_crawler_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        with pytest.raises(ValueError, match="private, local, or reserved"):
            await svc.ingest_url(user, url="http://intranet.local/admin")
        fake_crawler_client.crawl.assert_not_awaited()


class TestListDocs:
    async def test_returns_empty_list_without_a_brain(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        docs = await svc.list_docs(user)
        assert docs == []
        fake_morphik_client.list_docs.assert_not_awaited()

    async def test_lists_docs_for_the_workspace(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        record = DocumentRecord(
            id="doc-1",
            track_id="insert_1",
            status="processed",
            file_path="handbook.pdf",
            chunks_count=3,
            content_length=100,
            created_at="t1",
            updated_at="t2",
        )
        fake_morphik_client.list_docs.return_value = [record]

        docs = await svc.list_docs(user)

        assert docs == [record]
        fake_morphik_client.list_docs.assert_awaited_once_with(workspace="kai-v001-bob_at_test_com")
        fake_morphik_client.close.assert_awaited_once()


class TestDeleteDoc:
    async def test_raises_without_a_brain(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        with pytest.raises(ValueError):
            await svc.delete_doc(user, doc_id="doc-1")
        fake_morphik_client.delete_doc.assert_not_awaited()

    async def test_calls_client_and_closes(self, db, user, fake_morphik_client):
        svc = BrainsService(db)
        svc.create_brain(user)
        fake_morphik_client.delete_doc.return_value = "deletion_started"

        status = await svc.delete_doc(user, doc_id="doc-1")

        assert status == "deletion_started"
        fake_morphik_client.delete_doc.assert_awaited_once_with(
            doc_id="doc-1", workspace="kai-v001-bob_at_test_com"
        )
        fake_morphik_client.close.assert_awaited_once()
