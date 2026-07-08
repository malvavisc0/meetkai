import io
import json

import pytest
import respx
from httpx import Response

from kai.brain.client import (
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    DocumentRecord,
    IngestResult,
    LightRagClient,
    QueryResult,
)


@pytest.fixture
def client(settings):
    return LightRagClient(settings)


class TestIngestText:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_track_id(self, client):
        respx.post("/documents/text").mock(
            return_value=Response(
                200,
                json={
                    "status": "success",
                    "message": "Text successfully received.",
                    "track_id": "insert_20260706_075218_03b54836",
                },
            )
        )
        result = await client.ingest_text(
            file_source="onboarding-notes.txt",
            text="Refund policy: 30 days.",
            workspace="kai-test",
        )
        assert isinstance(result, IngestResult)
        assert result.track_id == "insert_20260706_075218_03b54836"
        assert result.status == "success"

        # Verify the body shape sent (file_source + workspace required)
        body = json.loads(respx.calls[0].request.content)
        assert body["file_source"] == "onboarding-notes.txt"
        assert body["workspace"] == "kai-test"
        assert body["text"] == "Refund policy: 30 days."

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_on_http_error(self, client):
        respx.post("/documents/text").mock(return_value=Response(500))
        with pytest.raises(Exception):
            await client.ingest_text(file_source="x", text="y", workspace="ws")


class TestIngestFile:
    @respx.mock
    @pytest.mark.asyncio
    async def test_uploads_multipart_with_workspace_query_param(self, client):
        respx.post("/documents/upload").mock(
            return_value=Response(
                200,
                json={
                    "status": "success",
                    "track_id": "upload_20260706_083651_45f299b1",
                },
            )
        )
        file_bytes = io.BytesIO(b"%PDF-1.4 ...")
        result = await client.ingest_file(
            file=file_bytes, filename="handbook.pdf", workspace="kai-test"
        )
        assert result.track_id == "upload_20260706_083651_45f299b1"

        # workspace must be a query param, NOT in the body.
        req = respx.calls[0].request
        assert "workspace=kai-test" in str(req.url)
        # multipart content-type
        assert "multipart/form-data" in req.headers.get("content-type", "")


class TestTrackStatus:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_documents_list(self, client):
        track_id = "insert_20260706_075218_03b54836"
        respx.get(f"/documents/track_status/{track_id}").mock(
            return_value=Response(
                200,
                json={
                    "track_id": track_id,
                    "documents": [
                        {
                            "id": "doc-79cc14d91766f64cdd6e4cae4e6d0cd5",
                            "status": "processed",
                            "file_path": "kai-refund-policy.txt",
                            "chunks_count": 1,
                            "content_length": 312,
                            "created_at": "2026-07-06T07:52:18+00:00",
                            "updated_at": "2026-07-06T07:52:24+00:00",
                            "metadata": {"parse_engine": "legacy"},
                        }
                    ],
                    "total_count": 1,
                },
            )
        )
        docs = await client.track_status(track_id=track_id)
        assert len(docs) == 1
        d = docs[0]
        assert isinstance(d, DocumentRecord)
        assert d.id == "doc-79cc14d91766f64cdd6e4cae4e6d0cd5"
        assert d.track_id == track_id
        assert d.status == "processed"
        assert d.file_path == "kai-refund-policy.txt"
        assert d.chunks_count == 1
        assert d.is_terminal is True
        assert d.metadata == {"parse_engine": "legacy"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_failed_status_is_terminal(self, client):
        track_id = "upload_20260706_083651_45f299b1"
        respx.get(f"/documents/track_status/{track_id}").mock(
            return_value=Response(
                200,
                json={
                    "track_id": track_id,
                    "documents": [
                        {
                            "id": "doc-3fcb9b343f00da18fa17f6792851860b",
                            "status": "failed",
                            "file_path": "test-upload.pdf",
                            "error_msg": "invalid literal for int()",
                            "content_length": 0,
                            "created_at": "",
                            "updated_at": "",
                        }
                    ],
                    "total_count": 1,
                },
            )
        )
        docs = await client.track_status(track_id=track_id)
        assert docs[0].status == DOC_STATUS_FAILED
        assert docs[0].is_terminal is True
        assert docs[0].error_msg == "invalid literal for int()"

    @respx.mock
    @pytest.mark.asyncio
    async def test_processing_status_not_terminal(self, client):
        track_id = "insert_1"
        respx.get(f"/documents/track_status/{track_id}").mock(
            return_value=Response(
                200,
                json={
                    "track_id": track_id,
                    "documents": [
                        {
                            "id": "doc-x",
                            "status": "processing",
                            "file_path": "f",
                            "chunks_count": None,
                            "content_length": 0,
                            "created_at": "",
                            "updated_at": "",
                        }
                    ],
                    "total_count": 1,
                },
            )
        )
        docs = await client.track_status(track_id=track_id)
        assert docs[0].status == DOC_STATUS_PROCESSING
        assert docs[0].is_terminal is False


class TestListDocs:
    @respx.mock
    @pytest.mark.asyncio
    async def test_posts_paginated_with_workspace_in_body(self, client):
        respx.post("/documents/paginated").mock(
            return_value=Response(
                200,
                json={
                    "documents": [
                        {
                            "id": "doc-1",
                            "track_id": "insert_1",
                            "status": "processed",
                            "file_path": "a.txt",
                            "chunks_count": 2,
                            "content_length": 100,
                            "created_at": "",
                            "updated_at": "",
                        },
                        {
                            "id": "doc-2",
                            "track_id": "insert_2",
                            "status": "processing",
                            "file_path": "b.txt",
                            "chunks_count": None,
                            "content_length": 50,
                            "created_at": "",
                            "updated_at": "",
                        },
                    ],
                    "pagination": {"total_count": 2},
                },
            )
        )
        docs = await client.list_docs(workspace="kai-test")
        assert len(docs) == 2
        assert docs[0].id == "doc-1"
        assert docs[0].status == "processed"

        # workspace must be in the BODY (hidden param), not query
        body = json.loads(respx.calls[0].request.content)
        assert body["workspace"] == "kai-test"

    @respx.mock
    @pytest.mark.asyncio
    async def test_status_filters_passes_through(self, client):
        respx.post("/documents/paginated").mock(
            return_value=Response(200, json={"documents": [], "pagination": {"total_count": 0}})
        )
        await client.list_docs(workspace="ws", status_filters=["failed"])
        body = json.loads(respx.calls[0].request.content)
        assert body["status_filters"] == ["failed"]


class TestDeleteDoc:
    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_doc_ids_array(self, client):
        respx.delete("/documents/delete_document").mock(
            return_value=Response(
                200,
                json={
                    "status": "deletion_started",
                    "message": "Deletion job started.",
                    "doc_id": "doc-79cc14d91766f64cdd6e4cae4e6d0cd5",
                },
            )
        )
        status = await client.delete_doc(
            doc_id="doc-79cc14d91766f64cdd6e4cae4e6d0cd5", workspace="kai-test"
        )
        assert status == "deletion_started"

        req = respx.calls[0].request
        assert "workspace=kai-test" in str(req.url)
        body = json.loads(req.content)
        # doc_ids is an ARRAY (batch delete supported)
        assert body["doc_ids"] == ["doc-79cc14d91766f64cdd6e4cae4e6d0cd5"]
        assert body["delete_file"] is True


class TestClearWorkspace:
    @respx.mock
    @pytest.mark.asyncio
    async def test_workspace_in_query_param(self, client):
        respx.delete("/documents").mock(
            return_value=Response(200, json={"status": "success", "message": "cleared"})
        )
        status = await client.clear_workspace(workspace="kai-test")
        assert status == "success"
        req = respx.calls[0].request
        assert "workspace=kai-test" in str(req.url)


class TestQuery:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_response_and_references(self, client):
        respx.post("/query").mock(
            return_value=Response(
                200,
                json={
                    "response": "Refunds are available within 30 days.",
                    "references": [
                        {"file_path": "handbook.pdf"},
                        {"file_path": "pricing.docx"},
                    ],
                },
            )
        )
        result = await client.query(
            query="What is the refund policy?",
            workspace="kai-test",
        )
        assert isinstance(result, QueryResult)
        assert "30 days" in result.response
        assert len(result.references) == 2
        assert result.references[0].file_path == "handbook.pdf"

        body = json.loads(respx.calls[0].request.content)
        assert body["mode"] == "mix"
        assert body["enable_rerank"] is True
        assert body["include_references"] is True
        assert body["workspace"] == "kai-test"

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_references_returns_empty_list(self, client):
        respx.post("/query").mock(
            return_value=Response(200, json={"response": "no context", "references": []})
        )
        result = await client.query(query="?", workspace="ws")
        assert result.references == []
        assert result.response == "no context"

    @respx.mock
    @pytest.mark.asyncio
    async def test_mode_override(self, client):
        respx.post("/query").mock(
            return_value=Response(200, json={"response": "ok", "references": []})
        )
        await client.query(query="?", workspace="ws", mode="local", enable_rerank=False)
        body = json.loads(respx.calls[0].request.content)
        assert body["mode"] == "local"
        assert body["enable_rerank"] is False


class TestHeaders:
    def test_api_key_header_set(self, settings):
        client = LightRagClient(settings)
        assert client._client.headers["X-API-Key"] == "lightrag-test-key"
