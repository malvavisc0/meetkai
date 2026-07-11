"""LightRagClient — typed async wrapper over the LightRAG v1.5.4 HTTP API.

All endpoints require ``X-API-Key`` (set on the client at construction, per
the WahaClient pattern). The ``workspace`` is passed per-call (not stored on
the client) because the cockpit's ``BrainsService`` derives it per-user
(``kai-v001-<sanitized-email>``), and the agent tool uses the deployment's
single workspace.

Every method, path, body, and response field here was validated empirically
against a running ``ghcr.io/hkuds/lightrag:v1.5.4`` container.
"""

from __future__ import annotations

import logging
from typing import Any, BinaryIO, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kai.brain.config import BrainSettings, get_brain_settings

logger = logging.getLogger(__name__)

# DocStatus enum — the 7 states returned by /documents/track_status.
# ``processed`` and ``failed`` are terminal; the rest are in-flight.
DOC_STATUS_PENDING = "pending"
DOC_STATUS_PARSING = "parsing"
DOC_STATUS_ANALYZING = "analyzing"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_PREPROCESSED = "preprocessed"
DOC_STATUS_PROCESSED = "processed"
DOC_STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({DOC_STATUS_PROCESSED, DOC_STATUS_FAILED})

# The field carries a Literal so an unexpected API value fails validation at
# the boundary instead of flowing through silently. The ``| str`` fallback
# keeps parsing resilient: LightRAG has added statuses before.
DocStatus = Literal[
    "pending",
    "parsing",
    "analyzing",
    "processing",
    "preprocessed",
    "processed",
    "failed",
]

# Query modes LightRAG supports (/query body).
QueryMode = Literal["naive", "local", "global", "hybrid", "mix"]


class DocumentRecord(BaseModel):
    """One document row from /documents/track_status or /documents/paginated.

    The fields mirror the v1.5.4 response shape. ``id`` is the
    LightRAG doc id (``doc-...``) used for delete; ``track_id`` is the ingest
    job id (``insert_...`` / ``upload_...``) used for status polling.
    """

    model_config = ConfigDict(frozen=True)

    id: str = ""
    track_id: str = ""
    status: DocStatus | str = ""
    file_path: str = ""
    chunks_count: int | None = None
    content_length: int = 0
    created_at: str = ""
    updated_at: str = ""
    error_msg: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content_length", mode="before")
    @classmethod
    def _default_none(cls, v: int | None) -> int:
        return v or 0

    @field_validator("metadata", mode="before")
    @classmethod
    def _default_metadata(cls, v: dict[str, Any] | None) -> dict[str, Any]:
        return v or {}

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_track_doc(cls, track_id: str, doc: dict[str, Any]) -> DocumentRecord:
        """Build from one element of track_status's documents[] list.

        ``track_id`` isn't in the per-doc payload for this endpoint, so it's
        injected before validation.
        """
        return cls.model_validate({**doc, "track_id": track_id})

    @classmethod
    def from_list_doc(cls, doc: dict[str, Any]) -> DocumentRecord:
        """Build from one element of paginated's documents[] list (track_id is present)."""
        return cls.model_validate(doc)


class IngestResult(BaseModel):
    """Returned by ingest_text / ingest_file / ingest_texts."""

    model_config = ConfigDict(frozen=True)

    track_id: str = ""
    status: str = ""
    message: str = ""

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> IngestResult:
        return cls.model_validate(data)


class QueryReference(BaseModel):
    """One source document cited in a /query response."""

    model_config = ConfigDict(frozen=True)

    file_path: str = ""


class QueryResult(BaseModel):
    """The result of a /query call."""

    model_config = ConfigDict(frozen=True)

    response: str = ""
    references: list[QueryReference] = Field(default_factory=list)

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> QueryResult:
        refs_raw = data.get("references") or []
        return cls(
            response=data.get("response", ""),
            references=[
                QueryReference.model_validate(r) for r in refs_raw if isinstance(r, dict)
            ],
        )


class LightRagClient:
    """Async HTTP client for LightRAG v1.5.4.

    Mirrors the WahaClient pattern: one ``httpx.AsyncClient`` with a base_url
    + auth header, constructed from BrainSettings. Callers pass ``workspace``
    per-method because it's user-scoped, not client-scoped.

    Lifecycle: build once (in ``cli/bot.py`` for the agent tool, or in
    ``BrainsService`` for the cockpit), ``await client.close()`` on shutdown.
    """

    def __init__(self, settings: BrainSettings | None = None) -> None:
        self.settings = settings or get_brain_settings()
        self.base_url = self.settings.base_url.rstrip("/")
        headers: dict[str, str] = {"X-API-Key": self.settings.lightrag_api_key}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=120.0,  # indexing can take minutes for large docs; queries are ~seconds
            headers=headers,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Document ingest
    # ------------------------------------------------------------------

    async def ingest_text(
        self,
        *,
        file_source: str,
        text: str,
        workspace: str,
    ) -> IngestResult:
        """POST /documents/text — ingest raw text.

        ``file_source`` is required by v1.5.4 (400 without it) and
        becomes the document's ``file_path`` (the display name).
        Returns a track_id; poll ``track_status`` until terminal.
        """
        resp = await self._client.post(
            "/documents/text",
            json={
                "file_source": file_source,
                "text": text,
                "workspace": workspace,
            },
        )
        resp.raise_for_status()
        return IngestResult.from_response(resp.json())

    async def ingest_texts(
        self,
        *,
        file_sources: list[str],
        texts: list[str],
        workspace: str,
    ) -> IngestResult:
        """POST /documents/texts — batch ingest (one track_id for the batch)."""
        resp = await self._client.post(
            "/documents/texts",
            json={
                "file_sources": file_sources,
                "texts": texts,
                "workspace": workspace,
            },
        )
        resp.raise_for_status()
        return IngestResult.from_response(resp.json())

    async def ingest_file(
        self,
        *,
        file: BinaryIO,
        filename: str,
        workspace: str,
    ) -> IngestResult:
        """POST /documents/upload — multipart file upload (txt/md/pdf/docx/…).

        ``workspace`` goes as a query param (not in the multipart body);
        confirmed working. The filename is passed through unchanged
        so LightRAG's parser-routing hints (e.g. ``paper.[mineru-iteP].pdf``)
        survive.
        """
        resp = await self._client.post(
            "/documents/upload",
            params={"workspace": workspace},
            files={"file": (filename, file)},
        )
        resp.raise_for_status()
        return IngestResult.from_response(resp.json())

    # ------------------------------------------------------------------
    # Document status / list / delete
    # ------------------------------------------------------------------

    async def track_status(self, *, track_id: str) -> list[DocumentRecord]:
        """GET /documents/track_status/{track_id} — poll ingest progress.

        Returns the documents[] list (usually one element, but batch
        ingest can yield several). Each record's ``status`` transitions
        pending → parsing → analyzing → processing → preprocessed →
        processed (or failed).
        """
        resp = await self._client.get(f"/documents/track_status/{track_id}")
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("documents") or []
        return [DocumentRecord.from_track_doc(track_id, d) for d in docs if isinstance(d, dict)]

    async def list_docs(
        self,
        *,
        workspace: str,
        page: int = 1,
        page_size: int = 50,
        status_filters: list[str] | None = None,
    ) -> list[DocumentRecord]:
        """POST /documents/paginated — list documents (NOT GET /documents).

        ``workspace`` is passed in the body (it's a hidden param).
        Returns the documents[] list; the caller can read total_count from
        the full response if needed via ``list_docs_raw``.
        """
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "sort_field": "updated_at",
            "sort_direction": "desc",
            "workspace": workspace,
        }
        if status_filters:
            body["status_filters"] = status_filters
        resp = await self._client.post("/documents/paginated", json=body)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("documents") or []
        return [DocumentRecord.from_list_doc(d) for d in docs if isinstance(d, dict)]

    async def status_counts(self, *, workspace: str) -> dict[str, int]:
        """GET /documents/status_counts?workspace= — per-status counts."""
        resp = await self._client.get("/documents/status_counts", params={"workspace": workspace})
        resp.raise_for_status()
        return resp.json()

    async def delete_doc(self, *, doc_id: str, workspace: str, delete_file: bool = True) -> str:
        """DELETE /documents/delete_document — async single-doc delete.

        Body is ``{"doc_ids": [...], "delete_file": bool}`` (the array
        supports batch delete). ``workspace`` is passed as a query param so
        the deletion is scoped to the operator's Brain — without it a user
        could delete a document from another workspace by guessing doc_ids.
        Returns the status string ("deletion_started"); deletion is async,
        poll ``list_docs`` until the doc is gone.

        NOTE: uses ``request("DELETE", ...)`` rather than ``delete()`` because
        httpx's ``delete()`` doesn't accept a ``json=`` kwarg (DELETE-with-body
        is non-standard), but LightRAG's v1.5.4 endpoint requires one.
        """
        resp = await self._client.request(
            "DELETE",
            "/documents/delete_document",
            params={"workspace": workspace},
            json={"doc_ids": [doc_id], "delete_file": delete_file},
        )
        resp.raise_for_status()
        return resp.json().get("status", "")

    async def clear_workspace(self, *, workspace: str) -> str:
        """DELETE /documents?workspace= — clears ALL docs in the workspace.

        DANGEROUS: removes every document. Used by tests and (eventually)
        a "reset Brain" admin action. Not exposed in the v1 UI.
        """
        resp = await self._client.request("DELETE", "/documents", params={"workspace": workspace})
        resp.raise_for_status()
        return resp.json().get("status", "")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def query(
        self,
        *,
        query: str,
        workspace: str,
        mode: QueryMode = "mix",
        enable_rerank: bool = True,
        include_references: bool = True,
    ) -> QueryResult:
        """POST /query — retrieve + synthesize a grounded answer.

        ``mode="mix"`` + ``enable_rerank=True`` is the validated default:
        hybrid vector + keyword retrieval, then cohere rerank, then
        LLM synthesis with references. The agent's ``brain_query`` tool calls
        this.
        """
        resp = await self._client.post(
            "/query",
            json={
                "query": query,
                "mode": mode,
                "enable_rerank": enable_rerank,
                "include_references": include_references,
                "workspace": workspace,
            },
        )
        resp.raise_for_status()
        return QueryResult.from_response(resp.json())
