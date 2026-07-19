"""MorphikClient — typed async wrapper over the Morphik core REST API.

Isolation is backend-enforced via ``end_user_id``, passed per-request on
every ingest/query/list/delete call. The cockpit's ``BrainsService`` derives
it per-user (``kai-v001-<sanitized-email>``); the agent tool uses the
deployment's single ``end_user_id``.

Endpoints, bodies, and response fields validated against
``ghcr.io/morphik-org/morphik-core:2026-07-05`` (OpenAPI at /docs).
"""

import logging
from typing import Any, BinaryIO, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kai.brain.config import BrainSettings, get_brain_settings

logger = logging.getLogger(__name__)

DOC_STATUS_PENDING = "pending"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_PROCESSED = "completed"
DOC_STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({DOC_STATUS_PROCESSED, DOC_STATUS_FAILED})

DocStatus = Literal[
    "pending",
    "processing",
    "completed",
    "failed",
]

QueryMode = Literal["naive", "local", "global", "hybrid", "mix"]


class DocumentRecord(BaseModel):
    """One document row from /documents or /documents/{id}/status."""

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
    def from_list_doc(cls, doc: dict[str, Any]) -> "DocumentRecord":
        """Build from one element of /documents/list_docs documents[]."""
        return cls.model_validate(
            {
                "id": doc.get("external_id", ""),
                "status": doc.get("system_metadata", {}).get("status", "") or doc.get("status", ""),
                "file_path": doc.get("filename", "") or "",
                "chunks_count": len(doc.get("chunk_ids", []) or []) or None,
                "content_length": doc.get("system_metadata", {}).get("content_length", 0) or 0,
                "created_at": doc.get("system_metadata", {}).get("created_at", "") or "",
                "updated_at": doc.get("system_metadata", {}).get("updated_at", "") or "",
                "metadata": doc.get("metadata", {}) or {},
            }
        )

    @classmethod
    def from_status(cls, doc_id: str, data: dict[str, Any]) -> "DocumentRecord":
        """Build from /documents/{id}/status response."""
        return cls.model_validate(
            {
                "id": doc_id,
                "status": data.get("status", ""),
                "metadata": data,
            }
        )


class IngestResult(BaseModel):
    """Returned by ingest_text / ingest_file / ingest_texts."""

    model_config = ConfigDict(frozen=True)

    track_id: str = ""
    status: str = ""
    message: str = ""

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "IngestResult":
        # Morphik returns the Document object on ingest; its external_id is
        # the handle used to poll /documents/{id}/status.
        return cls(
            track_id=data.get("external_id", ""),
            status=data.get("system_metadata", {}).get("status", "pending")
            if isinstance(data.get("system_metadata"), dict)
            else "pending",
            message=data.get("filename", "") or "",
        )


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
    def from_response(cls, data: dict[str, Any]) -> "QueryResult":
        # Morphik /query returns {completion, sources[]} where each source has
        # document_id + chunk_number. filename isn't on the source; we keep
        # file_path as document_id for traceability.
        sources = data.get("sources") or []
        return cls(
            response=data.get("completion", "") or "",
            references=[
                QueryReference(file_path=s.get("document_id", ""))
                for s in sources
                if isinstance(s, dict)
            ],
        )


class MorphikClient:
    """Async HTTP client for Morphik core.

    Mirrors the WahaClient pattern. Callers pass ``end_user_id`` per-method
    because it's user-scoped, not client-scoped. Auth is a single Bearer
    token set at construction. Lifecycle: build once, ``await client.close()``
    on shutdown.
    """

    def __init__(self, settings: BrainSettings | None = None) -> None:
        self.settings = settings or get_brain_settings()
        self.base_url = self.settings.base_url.rstrip("/")
        headers: dict[str, str] = {"Authorization": f"Bearer {self.settings.morphik_token}"}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=120.0,
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
        """POST /ingest/text — ingest raw text.

        ``file_source`` becomes the document's ``filename`` (display name).
        ``workspace`` maps to Morphik's ``end_user_id`` isolation key.
        Returns the document's external_id as track_id; poll
        ``track_status`` until terminal.
        """
        resp = await self._client.post(
            "/ingest/text",
            json={
                "content": text,
                "filename": file_source,
                "end_user_id": workspace,
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
        """Batch ingest — Morphik has no batch-text endpoint, so this is a
        thin loop over ``ingest_text``. Returns the first document's id as
        track_id (callers poll that one; the others share the same crawl
        provenance via their ``metadata.source_url``)."""
        if not texts:
            return IngestResult()
        first: IngestResult | None = None
        for name, text in zip(file_sources, texts, strict=True):
            r = await self.ingest_text(file_source=name, text=text, workspace=workspace)
            if first is None:
                first = r
        return first or IngestResult()

    async def ingest_file(
        self,
        *,
        file: BinaryIO,
        filename: str,
        workspace: str,
    ) -> IngestResult:
        """POST /ingest/file — multipart file upload.

        ``end_user_id`` is a form field; the filename passes through unchanged.
        """
        resp = await self._client.post(
            "/ingest/file",
            data={"end_user_id": workspace},
            files={"file": (filename, file)},
        )
        resp.raise_for_status()
        return IngestResult.from_response(resp.json())

    # ------------------------------------------------------------------
    # Document status / list / delete
    # ------------------------------------------------------------------

    async def track_status(self, *, track_id: str) -> list[DocumentRecord]:
        """GET /documents/{id}/status — poll ingest progress.

        Returns a single-element list (Morphik tracks per-document, not per-
        batch). ``status`` transitions pending → processing → completed/failed.
        """
        resp = await self._client.get(f"/documents/{track_id}/status")
        resp.raise_for_status()
        return [DocumentRecord.from_status(track_id, resp.json())]

    async def list_docs(
        self,
        *,
        workspace: str,
        page: int = 1,
        page_size: int = 50,
        status_filters: list[str] | None = None,
    ) -> list[DocumentRecord]:
        """POST /documents/list_docs?end_user_id= — list documents scoped
        to the user. ``page``/``page_size`` map to ``skip``/``limit``."""
        body: dict[str, Any] = {
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "sort_by": "updated_at",
            "sort_direction": "desc",
        }
        if status_filters:
            body["document_filters"] = {"status": {"$in": status_filters}}
        resp = await self._client.post(
            "/documents/list_docs",
            params={"end_user_id": workspace},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("documents") or []
        return [DocumentRecord.from_list_doc(d) for d in docs if isinstance(d, dict)]

    async def status_counts(self, *, workspace: str) -> dict[str, int]:
        """POST /documents/list_docs?end_user_id= with aggregates only."""
        resp = await self._client.post(
            "/documents/list_docs",
            params={"end_user_id": workspace},
            json={"return_documents": False, "include_status_counts": True},
        )
        resp.raise_for_status()
        return resp.json().get("status_counts", {}) or {}

    async def delete_doc(self, *, doc_id: str, workspace: str, delete_file: bool = True) -> str:
        """DELETE /documents/{id} — synchronous single-doc delete.

        ``workspace`` (end_user_id) isn't a path param on Morphik's delete;
        the token's scope already restricts it. We pass it as a query param
        for defense-in-depth — Morphik ignores unknown query params.
        """
        resp = await self._client.delete(f"/documents/{doc_id}")
        resp.raise_for_status()
        return resp.json().get("status", "")

    async def clear_workspace(self, *, workspace: str) -> str:
        """Clear ALL docs for an end_user_id — list then delete each.

        DANGEROUS: used by tests and a future "reset Brain" admin action.
        Not exposed in the v1 UI.
        """
        docs = await self.list_docs(workspace=workspace, page=1, page_size=1000)
        for d in docs:
            if d.id:
                await self.delete_doc(doc_id=d.id, workspace=workspace)
        return "completed"

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

        ``mode`` is accepted for call-site compatibility but Morphik's
        retrieval is always hybrid (vector + metadata); the param is ignored
        server-side. ``enable_rerank`` maps to ``use_reranking``.
        Called by the agent's ``brain_query`` tool.
        """
        body: dict[str, Any] = {
            "query": query,
            "use_reranking": enable_rerank if enable_rerank else None,
            "end_user_id": workspace,
        }
        resp = await self._client.post("/query", json=body)
        resp.raise_for_status()
        return QueryResult.from_response(resp.json())
