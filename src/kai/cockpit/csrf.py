import secrets
from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_csrf_token: ContextVar[str | None] = ContextVar("csrf_token", default=None)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CSRFMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if "session" not in scope:
            raise RuntimeError(
                "CSRFMiddleware requires SessionMiddleware to be added after it "
                "(outermost) so request.session is populated"
            )
        session = scope["session"]
        if scope["method"].upper() in _SAFE_METHODS:
            await self._handle_safe(scope, receive, send, session)
        else:
            await self._handle_unsafe(scope, receive, send, session)

    async def _handle_safe(self, scope: Scope, receive: Receive, send: Send, session: dict) -> None:
        # Ensure a token exists for templates to embed; mint one on first
        # visit so the very first form render already carries a valid token.
        token = session.get("csrf_token")
        if not isinstance(token, str) or not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        await self._call_with_token(scope, receive, send, token)

    async def _handle_unsafe(
        self, scope: Scope, receive: Receive, send: Send, session: dict
    ) -> None:
        token = session.get("csrf_token")
        context_token = token if isinstance(token, str) else None
        if self._is_exempt(scope["path"]):
            await self._call_with_token(scope, receive, send, context_token)
            return

        submitted = self._header_token(scope)
        body: bytes | None = None
        if submitted is None:
            body = await self._read_body(receive)
            submitted = await self._form_token(scope, body)

        if not self._matches(token, submitted):
            await self._invalid(scope, receive, send, context_token)
            return

        replay = self._replay(body) if body is not None else receive
        await self._call_with_token(scope, replay, send, context_token)

    async def _call_with_token(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        token: str | None,
    ) -> None:
        marker = _csrf_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _csrf_token.reset(marker)

    @staticmethod
    def _is_exempt(path: str) -> bool:
        # /webhook/{workspace_slug}/{type_name} is HMAC-verified machine
        # ingress (routes/connections/webhooks.py). The trailing slash after
        # "webhook" ensures we match the full prefix, not e.g. /webhookfoo.
        return path.startswith("/webhook/") or path == "/api/escalations"

    @staticmethod
    def _header_token(scope: Scope) -> str | None:
        for name, value in scope["headers"]:
            if name.lower() == b"x-csrf-token":
                return value.decode("latin-1")
        return None

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @classmethod
    async def _form_token(cls, scope: Scope, body: bytes) -> str | None:
        request = Request(scope, cls._replay(body))
        try:
            form = await request.form()
        except (ValueError, RuntimeError):
            return None
        token = form.get("_csrf")
        return token if isinstance(token, str) else None

    @staticmethod
    def _matches(expected: object, submitted: str | None) -> bool:
        return (
            isinstance(expected, str)
            and bool(expected)
            and submitted is not None
            and secrets.compare_digest(expected, submitted)
        )

    @classmethod
    async def _invalid(
        cls,
        scope: Scope,
        receive: Receive,
        send: Send,
        token: str | None,
    ) -> None:
        response = (
            JSONResponse({"detail": "CSRF token invalid"}, status_code=403)
            if scope["path"].startswith("/api/")
            else PlainTextResponse("CSRF token invalid", status_code=403)
        )
        marker = _csrf_token.set(token)
        try:
            await response(scope, receive, send)
        finally:
            _csrf_token.reset(marker)

    @staticmethod
    def _replay(body: bytes) -> Receive:
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive
