"""Signed session cookie, origin check, login rate limit and the require_session dependency."""

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
COOKIE_MAX_AGE = 30 * 24 * 3600
LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 300


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


class LoginRateLimiter:
    """Global sliding window; the NodePort hides client IPs so it cannot be per-IP."""

    def __init__(self, limit: int = LOGIN_LIMIT, window: float = LOGIN_WINDOW_SECONDS) -> None:
        self._limit = limit
        self._window = window
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self._window:
            self._hits.popleft()
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True


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
