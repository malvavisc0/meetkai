import io
import json

import httpx
import pytest
import respx
from httpx import Response

from kai.brain.client import (
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    DocumentRecord,
    IngestResult,
    MorphikClient,
    QueryResult,
)


@pytest.fixture
def client(settings):
    return MorphikClient(settings)


class TestIngestText:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_external_id(self, client):
        respx.post("/ingest/text").mock(
            return_value=Response(
                200,
                json={
                    "external_id": "doc-abc123",
                    "content_type": "text/plain",
                    "filename": "onboarding-notes.txt",
                    "system_metadata": {"status": "pending"},
                    "metadata": {},
                    "chunk_ids": [],
                },
            )
        )
        result = await client.ingest_text(
            file_source="onboarding-notes.txt",
            text="Refund policy: 30 days.",
            workspace="kai-test",
        )
        assert isinstance(result, IngestResult)
        assert result.track_id == "doc-abc123"
        assert result.status == "pending"

        # Verify the body shape sent (content + filename + end_user_id)
        body = json.loads(respx.calls[0].request.content)
        assert body["content"] == "Refund policy: 30 days."
        assert body["filename"] == "onboarding-notes.txt"
        assert body["end_user_id"] == "kai-test"

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_on_http_error(self, client):
        respx.post("/ingest/text").mock(return_value=Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await client.ingest_text(file_source="x", text="y", workspace="ws")


class TestIngestFile:
    @respx.mock
    @pytest.mark.asyncio
    async def test_uploads_multipart_with_end_user_id_form_field(self, client):
        respx.post("/ingest/file").mock(
            return_value=Response(
                200,
                json={
                    "external_id": "doc-upload456",
                    "content_type": "application/pdf",
                    "filename": "handbook.pdf",
                    "system_metadata": {"status": "pending"},
                    "metadata": {},
                    "chunk_ids": [],
                },
            )
        )
        file_bytes = io.BytesIO(b"%PDF-1.4 ...")
        result = await client.ingest_file(
            file=file_bytes, filename="handbook.pdf", workspace="kai-test"
        )
        assert result.track_id == "doc-upload456"

        # end_user_id is a multipart form field, NOT a query param.
        req = respx.calls[0].request
        body_text = req.content.decode("utf-8", errors="ignore")
        assert 'name="end_user_id"' in body_text
        assert "kai-test" in body_text
        assert "multipart/form-data" in req.headers.get("content-type", "")


class TestIngestTexts:
    @respx.mock
    @pytest.mark.asyncio
    async def test_batch_loops_ingest_text(self, client):
        # Two sequential ingest_text calls; respx matches any /ingest/text.
        respx.post("/ingest/text").mock(
            return_value=Response(
                200,
                json={
                    "external_id": "doc-first",
                    "content_type": "text/plain",
                    "system_metadata": {"status": "pending"},
                    "metadata": {},
                    "chunk_ids": [],
                },
            )
        )
        result = await client.ingest_texts(
            file_sources=["page-1", "page-2"],
            texts=["content 1", "content 2"],
            workspace="kai-test",
        )
        # Returns the first doc's id as track_id
        assert result.track_id == "doc-first"
        # Both pages were ingested (two calls)
        assert len(respx.calls) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_batch_returns_empty(self, client):
        result = await client.ingest_texts(file_sources=[], texts=[], workspace="kai-test")
        assert result.track_id == ""
        assert len(respx.calls) == 0


class TestTrackStatus:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_single_doc_list(self, client):
        doc_id = "doc-abc123"
        respx.get(f"/documents/{doc_id}/status").mock(
            return_value=Response(
                200,
                json={"status": "completed", "progress": 100},
            )
        )
        docs = await client.track_status(track_id=doc_id)
        assert len(docs) == 1
        d = docs[0]
        assert isinstance(d, DocumentRecord)
        assert d.id == doc_id
        assert d.status == "completed"
        assert d.is_terminal is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_failed_status_is_terminal(self, client):
        doc_id = "doc-failed1"
        respx.get(f"/documents/{doc_id}/status").mock(
            return_value=Response(200, json={"status": "failed", "error": "parse error"})
        )
        docs = await client.track_status(track_id=doc_id)
        assert docs[0].status == DOC_STATUS_FAILED
        assert docs[0].is_terminal is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_processing_status_not_terminal(self, client):
        doc_id = "doc-proc1"
        respx.get(f"/documents/{doc_id}/status").mock(
            return_value=Response(200, json={"status": "processing"})
        )
        docs = await client.track_status(track_id=doc_id)
        assert docs[0].status == DOC_STATUS_PROCESSING
        assert docs[0].is_terminal is False


class TestListDocs:
    @respx.mock
    @pytest.mark.asyncio
    async def test_posts_list_docs_with_end_user_id_query(self, client):
        respx.post("/documents/list_docs").mock(
            return_value=Response(
                200,
                json={
                    "documents": [
                        {
                            "external_id": "doc-1",
                            "content_type": "text/plain",
                            "filename": "a.txt",
                            "system_metadata": {
                                "status": "completed",
                                "content_length": 100,
                                "created_at": "2026-07-06T00:00:00Z",
                                "updated_at": "2026-07-06T00:01:00Z",
                            },
                            "chunk_ids": ["c1", "c2"],
                            "metadata": {"source": "upload"},
                        },
                        {
                            "external_id": "doc-2",
                            "content_type": "text/plain",
                            "filename": "b.txt",
                            "system_metadata": {"status": "processing"},
                            "chunk_ids": [],
                            "metadata": {},
                        },
                    ],
                    "skip": 0,
                    "limit": 50,
                    "returned_count": 2,
                },
            )
        )
        docs = await client.list_docs(workspace="kai-test")
        assert len(docs) == 2
        assert docs[0].id == "doc-1"
        assert docs[0].status == "completed"
        assert docs[0].file_path == "a.txt"
        assert docs[0].chunks_count == 2
        assert docs[0].metadata == {"source": "upload"}

        # end_user_id must be a query param.
        req = respx.calls[0].request
        assert "end_user_id=kai-test" in str(req.url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_status_filters_maps_to_document_filters(self, client):
        respx.post("/documents/list_docs").mock(
            return_value=Response(
                200, json={"documents": [], "skip": 0, "limit": 50, "returned_count": 0}
            )
        )
        await client.list_docs(workspace="ws", status_filters=["failed"])
        body = json.loads(respx.calls[0].request.content)
        assert body["document_filters"] == {"status": {"$in": ["failed"]}}


class TestDeleteDoc:
    @respx.mock
    @pytest.mark.asyncio
    async def test_deletes_by_path_id(self, client):
        respx.delete("/documents/doc-abc123").mock(
            return_value=Response(200, json={"status": "deleted", "message": "ok"})
        )
        status = await client.delete_doc(doc_id="doc-abc123", workspace="kai-test")
        assert status == "deleted"


class TestQuery:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_completion_and_sources(self, client):
        respx.post("/query").mock(
            return_value=Response(
                200,
                json={
                    "completion": "Refunds are available within 30 days.",
                    "usage": {"total_tokens": 50},
                    "sources": [
                        {"document_id": "doc-handbook", "chunk_number": 0, "score": 0.9},
                        {"document_id": "doc-pricing", "chunk_number": 2, "score": 0.8},
                    ],
                },
            )
        )
        result = await client.query(query="What is the refund policy?", workspace="kai-test")
        assert isinstance(result, QueryResult)
        assert "30 days" in result.response
        assert len(result.references) == 2
        assert result.references[0].file_path == "doc-handbook"

        body = json.loads(respx.calls[0].request.content)
        assert body["query"] == "What is the refund policy?"
        assert body["end_user_id"] == "kai-test"
        assert body["use_reranking"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_sources_returns_empty_list(self, client):
        respx.post("/query").mock(
            return_value=Response(200, json={"completion": "no context", "sources": []})
        )
        result = await client.query(query="?", workspace="ws")
        assert result.references == []
        assert result.response == "no context"

    @respx.mock
    @pytest.mark.asyncio
    async def test_disable_rerank_sends_null(self, client):
        respx.post("/query").mock(
            return_value=Response(200, json={"completion": "ok", "sources": []})
        )
        await client.query(query="?", workspace="ws", enable_rerank=False)
        body = json.loads(respx.calls[0].request.content)
        assert body["use_reranking"] is None


class TestHeaders:
    def test_bearer_token_header_set(self, settings):
        client = MorphikClient(settings)
        assert client._client.headers["Authorization"] == "Bearer morphik-test-token"
