import secrets
from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_TOKEN: ContextVar[str | None] = ContextVar("csrf_token", default=None)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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
        if scope["method"].upper() in SAFE_METHODS:
            await self.handle_safe(scope, receive, send, session)
        else:
            await self.handle_unsafe(scope, receive, send, session)

    async def handle_safe(self, scope: Scope, receive: Receive, send: Send, session: dict) -> None:
        """Mint the session token if absent and pass the request through."""
        # Ensure a token exists for templates to embed; mint one on first
        # visit so the very first form render already carries a valid token.
        token = session.get("csrf_token")
        if not isinstance(token, str) or not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        await self.call_with_token(scope, receive, send, token)

    async def handle_unsafe(
        self, scope: Scope, receive: Receive, send: Send, session: dict
    ) -> None:
        """Verify the submitted token, rejecting unsafe requests without a match."""
        token = session.get("csrf_token")
        context_token = token if isinstance(token, str) else None
        if self.is_exempt(scope["path"]):
            await self.call_with_token(scope, receive, send, context_token)
            return

        submitted = self.header_token(scope)
        body: bytes | None = None
        if submitted is None:
            body = await self.read_body(receive)
            submitted = await self.form_token(scope, body)

        if not self.matches(token, submitted):
            await self.invalid(scope, receive, send, context_token)
            return

        replay = self.replay_body(body) if body is not None else receive
        await self.call_with_token(scope, replay, send, context_token)

    async def call_with_token(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        token: str | None,
    ) -> None:
        """Run the app with the CSRF token exposed to templates."""
        marker = CSRF_TOKEN.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            CSRF_TOKEN.reset(marker)

    @staticmethod
    def is_exempt(path: str) -> bool:
        """Report whether a path skips CSRF checks (machine ingress)."""
        # /webhook/{workspace_slug}/{type_name} is HMAC-verified machine
        # ingress (routes/connections/webhooks.py). The trailing slash after
        # "webhook" ensures we match the full prefix, not e.g. /webhookfoo.
        return path.startswith("/webhook/") or path == "/api/escalations"

    @staticmethod
    def header_token(scope: Scope) -> str | None:
        """Read the CSRF token from the request headers, if present."""
        for name, value in scope["headers"]:
            if name.lower() == b"x-csrf-token":
                return value.decode("latin-1")
        return None

    @staticmethod
    async def read_body(receive: Receive) -> bytes:
        """Read and buffer the full request body."""
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
    async def form_token(cls, scope: Scope, body: bytes) -> str | None:
        """Read the CSRF token from the submitted form body, if present."""
        request = Request(scope, cls.replay_body(body))
        try:
            form = await request.form()
        except (ValueError, RuntimeError):
            return None
        token = form.get("_csrf")
        return token if isinstance(token, str) else None

    @staticmethod
    def matches(expected: object, submitted: str | None) -> bool:
        """Compare tokens in constant time, requiring both to be non-empty."""
        return (
            isinstance(expected, str)
            and bool(expected)
            and submitted is not None
            and secrets.compare_digest(expected, submitted)
        )

    @classmethod
    async def invalid(
        cls,
        scope: Scope,
        receive: Receive,
        send: Send,
        token: str | None,
    ) -> None:
        """Send a 403 response for a missing or mismatched token."""
        response = (
            JSONResponse({"detail": "CSRF token invalid"}, status_code=403)
            if scope["path"].startswith("/api/")
            else PlainTextResponse("CSRF token invalid", status_code=403)
        )
        marker = CSRF_TOKEN.set(token)
        try:
            await response(scope, receive, send)
        finally:
            CSRF_TOKEN.reset(marker)

    @staticmethod
    def replay_body(body: bytes) -> Receive:
        """Return a receive callable that replays the buffered body once."""
        sent = False

        async def receive() -> Message:
            """Replay the buffered request body exactly once."""
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive
