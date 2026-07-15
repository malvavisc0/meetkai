"""BrainsService — Brain (LightRAG workspace) provisioning + operator config
+ document management (upload / paste text / crawl a URL / list / delete).
"""

from __future__ import annotations

import re
from collections import deque
from typing import BinaryIO
from urllib.parse import urldefrag, urljoin, urlparse

from sqlalchemy.orm import Session

from kai.brain.client import DocumentRecord, IngestResult, LightRagClient
from kai.brain.config import get_brain_settings
from kai.brain.crawler import Crawl4aiClient
from kai.brain.validation import (
    validate_ingest_url,
    validate_upload_filename,
    validate_upload_size,
)
from kai.cockpit.models import Connection, Deployment, User
from kai.utils.common import now_iso, user_slug

_NON_SLUG_CHARS = re.compile(r"[^a-zA-Z0-9]+")


def _slug_for_url(url: str) -> str:
    """Derive a stable, readable ``file_source`` name from a URL."""
    parsed = urlparse(url)
    stem = f"{parsed.netloc}{parsed.path}".strip("/")
    slug = _NON_SLUG_CHARS.sub("-", stem).strip("-")
    return slug or url


def _normalize_internal_link(href: str, page_url: str, seed_host: str) -> str | None:
    """Resolve a discovered link against its page to an absolute same-host URL.

    Returns ``None`` for anything that should not enter the BFS frontier:
    non-http(s) schemes (mailto:, tel:, javascript:), different hosts
    (external links crawled off-host), or fragment-only references. The
    fragment is stripped (``/docs#section`` == ``/docs``) so the visited-set
    dedup treats them as the same page.
    """
    absolute = urljoin(page_url, href)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc != seed_host:
        return None
    return absolute


class BrainsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_brain(self, user: User) -> Connection | None:
        """Get the user's Brain (lightrag) connection row, or None."""
        return (
            self.db.query(Connection)
            .filter(Connection.user_id == user.id, Connection.service == "lightrag")
            .first()
        )

    def create_brain(self, user: User) -> Connection:
        """Provision the user's Brain: allocate a workspace, write the row.

        Idempotent — calling this again for a user who already has a Brain
        just returns the existing row unchanged (mirrors
        ``ConnectionsService.get_or_create_whatsapp``'s idempotency, but
        this one is reached only via an explicit "Create my Brain" click,
        never lazily from an unrelated flow).

        No external HTTP call is made: LightRAG workspaces aren't created
        via an API — a workspace is just a partition key the shared
        LightRAG container recognizes on first write via ``workspace=`` on
        ingest/query calls. So "creating" a Brain is purely a kai-side
        row; the workspace comes into existence in LightRAG the moment the
        first document is ingested into it.
        """
        existing = self.get_brain(user)
        if existing is not None:
            return existing

        conn = Connection(
            user_id=user.id,
            service="lightrag",
            status="ready",
            config={
                "workspace": user_slug(user.kai_slug),
                "instruction": "",
            },
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self.db.add(conn)
        self.db.commit()
        self.db.refresh(conn)
        return conn

    def update_instruction(self, user: User, *, instruction: str) -> Connection:
        """Save the operator's "when to use the Brain" free text.

        Raises ``ValueError`` if the user has no Brain yet — the website
        should only expose this form once a Brain exists.

        The instruction is injected into bot processes as
        ``KAI_BRAIN_INSTRUCTION`` at startup (``DeploymentsService.start``).
        A running bot won't see the new value until it restarts, so any
        running deployments are flagged ``needs_restart=True`` — the same
        pattern the deployment-settings edit uses.
        """
        conn = self.get_brain(user)
        if conn is None:
            raise ValueError("No Brain provisioned for this Operator yet.")

        conn.config = {
            **conn.config,
            "instruction": instruction,
        }
        conn.updated_at = now_iso()

        running = (
            self.db.query(Deployment)
            .filter(
                Deployment.user_id == user.id,
                Deployment.status == "running",
            )
            .all()
        )
        for dep in running:
            dep.needs_restart = True
            dep.updated_at = now_iso()

        self.db.commit()
        self.db.refresh(conn)
        return conn

    def delete_brain(self, user: User) -> None:
        """Remove the user's Brain connection row (kai-side only).

        Does NOT clear the workspace's documents/vectors inside LightRAG —
        that is ``LightRagClient.clear_workspace`` (a separate, explicit,
        dangerous admin action per ``brain/client.py``), not part of this
        lightweight "un-provision the row" flow.
        """
        conn = self.get_brain(user)
        if conn is None:
            return
        self.db.delete(conn)
        self.db.commit()

    # --- External clients (shared lightrag/crawl4ai containers) ---

    def _lightrag_client(self) -> LightRagClient:
        return LightRagClient(get_brain_settings())

    def _crawler_client(self) -> Crawl4aiClient:
        return Crawl4aiClient(get_brain_settings())

    def _require_brain(self, user: User) -> Connection:
        conn = self.get_brain(user)
        if conn is None:
            raise ValueError("No Brain provisioned for this Operator yet. Create the Brain first.")
        return conn

    # --- Document ingest ---

    async def ingest_text(self, user: User, *, name: str, text: str) -> IngestResult:
        """Ingest pasted/raw text under the operator-supplied ``name``.

        ``name`` becomes LightRAG's ``file_source`` — the single
        display name shown in the Documents table.
        """
        brain = self._require_brain(user)
        client = self._lightrag_client()
        try:
            return await client.ingest_text(
                file_source=name, text=text, workspace=brain.config["workspace"]
            )
        finally:
            await client.close()

    async def ingest_file(self, user: User, *, filename: str, file: BinaryIO) -> IngestResult:
        """Ingest an uploaded file.

        Raises ``ValueError`` if the file extension isn't in the ingest
        allowlist, or if the file is empty or over the size cap — checked
        here (before any network I/O against LightRAG) so an untyped or
        oversized upload never reaches the shared LightRAG container.
        """
        validate_upload_filename(filename)
        validate_upload_size(file)
        brain = self._require_brain(user)
        client = self._lightrag_client()
        try:
            return await client.ingest_file(
                file=file, filename=filename, workspace=brain.config["workspace"]
            )
        finally:
            await client.close()

    async def ingest_url(self, user: User, *, url: str) -> IngestResult:
        """Whole-site BFS crawl: fetch the seed page and, up to
        ``crawl_max_depth`` hops (seed = depth 0), every same-host page
        linked from it — capped at ``crawl_max_pages`` fetched pages.

        crawl4ai 0.9.0 rejects ``deep_crawl_strategy`` over HTTP, so
        the BFS is kai-orchestrated: each page is fetched via ``crawl()``
        (which returns the page markdown + its discovered internal links),
        and the returned ``links.internal`` drives the frontier. Only
        same-host links are followed; the seed host gates the frontier.

        All fetched pages are batch-ingested into LightRAG via
        ``ingest_texts`` (one track_id for the whole crawl) under per-page
        ``file_source`` slugs (e.g. ``transmissionbt-com-download``).

        Raises ``ValueError`` (before any crawl4ai call) if ``url`` isn't
        http(s), has no host, or resolves to a private/loopback/link-local
        (incl. cloud metadata)/reserved address — see
        ``kai.brain.validation.validate_ingest_url``. Same-host links
        discovered during the BFS are re-validated before being fetched,
        since ``_normalize_internal_link`` only checks scheme + hostname
        match, not the address they resolve to.
        """
        await validate_ingest_url(url)
        brain = self._require_brain(user)
        settings = get_brain_settings()
        seed_host = urlparse(url).netloc
        crawler = self._crawler_client()
        pages: list[tuple[str, str]] = []  # (url, markdown) of successful fetches
        try:
            fetched = 0
            visited: set[str] = {url}
            queue: deque[tuple[str, int]] = deque([(url, 0)])
            while queue and fetched < settings.crawl_max_pages:
                page_url, depth = queue.popleft()
                try:
                    await validate_ingest_url(page_url)
                except ValueError:
                    # A discovered link resolves somewhere unsafe (or its DNS
                    # changed since the seed check) — skip it, don't abort
                    # the whole crawl.
                    continue
                page = await crawler.crawl(url=page_url)
                fetched += 1
                # Only ingest and follow links from pages that actually
                # fetched successfully — a failed page can return stale or
                # garbage links that would send the BFS off-site.
                if not (page.success and page.markdown.raw_markdown.strip()):
                    continue
                pages.append((page_url, page.markdown.raw_markdown))
                if depth >= settings.crawl_max_depth:
                    continue
                for href in page.links.internal:
                    next_url = _normalize_internal_link(href, page_url, seed_host)
                    if next_url is not None and next_url not in visited:
                        visited.add(next_url)
                        queue.append((next_url, depth + 1))
        finally:
            await crawler.close()

        if not pages:
            raise ValueError(f"could not extract any content from {url}")

        file_sources = [_slug_for_url(u) for u, _ in pages]
        texts = [md for _, md in pages]
        client = self._lightrag_client()
        try:
            result = await client.ingest_texts(
                file_sources=file_sources, texts=texts, workspace=brain.config["workspace"]
            )
        finally:
            await client.close()
        n = len(pages)
        return IngestResult(
            track_id=result.track_id,
            status=result.status,
            message=f"{n} page{'s' if n != 1 else ''} fetched",
        )

    async def list_docs(self, user: User) -> list[DocumentRecord]:
        """List the Brain's documents (most recently updated first).

        Returns an empty list if the user has no Brain yet — callers render
        the empty state instead, this never raises for that case.
        """
        brain = self.get_brain(user)
        if brain is None:
            return []
        client = self._lightrag_client()
        try:
            return await client.list_docs(workspace=brain.config["workspace"])
        finally:
            await client.close()

    async def delete_doc(self, user: User, *, doc_id: str) -> str:
        """Delete one document from the Brain, scoped to its workspace."""
        brain = self._require_brain(user)
        client = self._lightrag_client()
        try:
            return await client.delete_doc(doc_id=doc_id, workspace=brain.config["workspace"])
        finally:
            await client.close()
