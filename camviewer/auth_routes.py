"""Login, logout, forget-account and the public session probe."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from camviewer.app_context import AppContext
from camviewer.hik_account import AuthError, CaptchaError
from camviewer.session_auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
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


def _set_cookie(response: Response, ctx: AppContext) -> None:
    # Secure stays off: the viewer is plain http on the LAN. Enable behind HTTPS.
    response.set_cookie(
        COOKIE_NAME,
        ctx.signer.issue(ctx.state.epoch),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )


@router.get("/session")
async def session_probe(request: Request) -> dict:
    """Public: tells the login page whether to show the form."""
    ctx = _ctx(request)
    return {
        "authenticated": has_session(ctx, request.cookies.get(COOKIE_NAME)),
        "account_status": ctx.state.status,
    }


@router.post("/login", dependencies=[Depends(require_post_origin)])
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    ctx = _ctx(request)
    if not ctx.limiter.allow():
        raise HTTPException(429, "too many login attempts, retry in a few minutes")
    stored = await asyncio.to_thread(ctx.store.username)
    signed_in = has_session(ctx, request.cookies.get(COOKIE_NAME))
    if stored and stored != body.username and not signed_in:
        raise HTTPException(409, "another account is already configured")
    try:
        await asyncio.to_thread(ctx.account.login, body.username, body.password)
    except CaptchaError as exc:
        raise HTTPException(
            423, "Hik-Connect requires CAPTCHA: log in once in the official app, then retry"
        ) from exc
    except AuthError as exc:
        raise HTTPException(401, "invalid Hik-Connect username or password") from exc
    except Exception as exc:  # noqa: BLE001 - network/cloud outage must not leak details
        log.warning("login failed: %s", type(exc).__name__)
        raise HTTPException(502, "could not reach Hik-Connect") from exc
    if stored and stored != body.username:
        await asyncio.to_thread(ctx.manager.stop_all)
    await asyncio.to_thread(ctx.store.save, body.username, body.password)
    _set_cookie(response, ctx)
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(require_post_origin)])
async def logout(request: Request, response: Response) -> dict:
    ctx = _ctx(request)
    ctx.state.bump_epoch()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post(
    "/account/forget",
    dependencies=[Depends(require_post_origin), Depends(require_session)],
)
async def forget_account(request: Request, response: Response) -> dict:
    ctx = _ctx(request)
    await asyncio.to_thread(ctx.manager.stop_all)
    ctx.store.forget()
    ctx.account.forget()
    ctx.state.bump_epoch()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
