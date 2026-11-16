"""App login (own account, not Hik-Connect), logout and the Hik-Connect retry hook."""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from camviewer.app_context import AppContext
from camviewer.hash_password import verify_password
from camviewer.hik_startup_login import restart_login
from camviewer.session_auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    client_ip,
    has_session,
    require_post_origin,
    require_session,
)

log = logging.getLogger("camviewer.auth")
router = APIRouter(prefix="/api")


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _credentials_match(ctx: AppContext, username: str, password: str) -> bool:
    """Always runs the password hash so a wrong username costs the same time."""
    user_ok = hmac.compare_digest(username.encode(), ctx.settings.app_username.encode())
    pass_ok = verify_password(password, ctx.settings.app_password_hash)
    return user_ok and pass_ok


@router.get("/session")
async def session_probe(request: Request) -> dict:
    """Public: tells the login page whether to show the form."""
    ctx = _ctx(request)
    return {"authenticated": has_session(ctx, request.cookies.get(COOKIE_NAME))}


@router.post("/login", dependencies=[Depends(require_post_origin)])
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    ctx = _ctx(request)
    if not ctx.limiter.allow(client_ip(request)):
        raise HTTPException(429, "too many login attempts, retry in a few minutes")
    ok = await asyncio.to_thread(_credentials_match, ctx, body.username, body.password)
    if not ok:
        raise HTTPException(401, "invalid username or password")
    response.set_cookie(
        COOKIE_NAME,
        ctx.signer.issue(ctx.state.epoch),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=ctx.settings.cookie_secure,
        path="/",
    )
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(require_post_origin)])
async def logout(request: Request, response: Response) -> dict:
    ctx = _ctx(request)
    ctx.state.bump_epoch()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post(
    "/account/retry",
    dependencies=[Depends(require_post_origin), Depends(require_session)],
)
async def retry_hik_login(request: Request) -> dict:
    """Try the Hik-Connect login again after the breaker or CAPTCHA stopped it."""
    ctx = _ctx(request)
    await asyncio.to_thread(ctx.manager.stop_all)
    restart_login(ctx)
    return {"ok": True}
