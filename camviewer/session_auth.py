"""Signed session cookie (sliding), origin check, login rate limits, require_session."""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, URLSafeTimedSerializer

if TYPE_CHECKING:
    from camviewer.app_context import AppContext

COOKIE_NAME = "camviewer_session"
COOKIE_MAX_AGE = 400 * 24 * 3600
LOGIN_GLOBAL_LIMIT = 10
LOGIN_IP_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300
MAX_TRACKED_IPS = 1000


class SessionSigner:
    """Cookie payload is only the epoch; bumping the stored epoch revokes every cookie."""

    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt="camviewer-session")

    def issue(self, epoch: int) -> str:
        return self._serializer.dumps({"epoch": epoch})

    def verify(self, token: str | None, epoch: int) -> bool:
        if not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=COOKIE_MAX_AGE)
        except BadSignature:
            return False
        return isinstance(data, dict) and data.get("epoch") == epoch


class _Window:
    def __init__(self, limit: int, window: float) -> None:
        self._limit, self._window = limit, window
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self._window:
            self._hits.popleft()
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True


class LoginRateLimiter:
    """Global sliding window plus a stricter per-IP window."""

    def __init__(
        self,
        global_limit: int = LOGIN_GLOBAL_LIMIT,
        ip_limit: int = LOGIN_IP_LIMIT,
        window: float = LOGIN_WINDOW_SECONDS,
    ) -> None:
        self._global = _Window(global_limit, window)
        self._ip_limit, self._window = ip_limit, window
        self._ips: dict[str, _Window] = {}

    def allow(self, ip: str) -> bool:
        if len(self._ips) > MAX_TRACKED_IPS:
            self._ips.clear()
        per_ip = self._ips.setdefault(ip, _Window(self._ip_limit, self._window))
        return per_ip.allow() and self._global.allow()


def client_ip(request: Request) -> str:
    """X-Real-IP set by the host nginx (overwritten there from $remote_addr), else the peer."""
    peer = request.client.host if request.client else "?"
    return request.headers.get("x-real-ip") or peer


def origin_matches_host(origin: str | None, host: str | None) -> bool:
    """Browsers always send Origin on POST and WebSocket; it must name this very host."""
    if not origin or not host:
        return False
    return urlparse(origin).netloc == host


def check_origin(request: Request) -> None:
    if not origin_matches_host(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(403, "bad origin")


def has_session(ctx: AppContext, cookie: str | None) -> bool:
    return ctx.signer.verify(cookie, ctx.state.epoch)


def require_session(request: Request) -> None:
    ctx: AppContext = request.app.state.ctx
    if not has_session(ctx, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "login required")


def require_post_origin(request: Request) -> None:
    """Dependency for state-changing routes."""
    check_origin(request)


def websocket_allowed(ctx: AppContext, ws: WebSocket) -> bool:
    return origin_matches_host(
        ws.headers.get("origin"), ws.headers.get("host")
    ) and has_session(ctx, ws.cookies.get(COOKIE_NAME))


def cookie_header(token: str, *, secure: bool) -> str:
    parts = [
        f"{COOKIE_NAME}={token}",
        f"Max-Age={COOKIE_MAX_AGE}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _cookie_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name == b"cookie":
            for item in value.decode("latin-1").split(";"):
                key, _, val = item.strip().partition("=")
                if key == COOKIE_NAME:
                    return val
    return None


class SlidingSessionMiddleware:
    """Re-issues the session cookie on every request that carries a valid one.

    Done at the ASGI level so streaming responses are untouched. Logout bumps the epoch,
    so the post-response re-check naturally skips the refresh there.
    """

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGI app
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _cookie_from_headers(scope["headers"])

        async def send_with_cookie(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.start" and token:
                ctx = scope["app"].state.ctx
                headers = message.setdefault("headers", [])
                already = any(
                    k == b"set-cookie" and v.startswith(COOKIE_NAME.encode()) for k, v in headers
                )
                if not already and has_session(ctx, token):
                    fresh = ctx.signer.issue(ctx.state.epoch)
                    value = cookie_header(fresh, secure=ctx.settings.cookie_secure)
                    headers.append((b"set-cookie", value.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_cookie)
