"""Authenticated, allowlisted reverse proxy to the internal go2rtc API.

Every upstream URL is rebuilt from a validated stream name (or HLS session id): the raw
query string never reaches go2rtc, so `src=exec:...` style injection is impossible.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

from camviewer.app_context import AppContext
from camviewer.go2rtc_client import stream_names
from camviewer.live_ingest_manager import stream_id_for
from camviewer.session_auth import require_session, websocket_allowed

router = APIRouter(prefix="/go2rtc/api")
HLS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HLS_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HTTP_PASSTHROUGH_HEADERS = ("content-type", "cache-control")


async def allowed_names(ctx: AppContext) -> set[str]:
    """Stream names that belong to the account's cameras."""
    cameras = await asyncio.to_thread(ctx.account.cameras)
    names: set[str] = set()
    for camera in cameras:
        names.update(stream_names(stream_id_for(camera)))
    return names


async def _checked_src(ctx: AppContext, src: str | None) -> str:
    if not src or src not in await allowed_names(ctx):
        raise HTTPException(403, "stream not allowed")
    return src


async def _forward(ctx: AppContext, path: str, params: dict[str, str]) -> Response:
    url = f"{ctx.settings.go2rtc_url}/api/{path}?{urlencode(params)}"
    request = ctx.http.build_request("GET", url)
    upstream = await ctx.http.send(request, stream=True)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    headers = {k: v for k, v in upstream.headers.items() if k in HTTP_PASSTHROUGH_HEADERS}
    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


@router.get("/frame.jpeg")
async def frame(request: Request, src: str | None = None) -> Response:
    require_session(request)
    ctx: AppContext = request.app.state.ctx
    return await _forward(ctx, "frame.jpeg", {"src": await _checked_src(ctx, src)})


@router.get("/stream.m3u8")
async def playlist(request: Request, src: str | None = None) -> Response:
    require_session(request)
    ctx: AppContext = request.app.state.ctx
    return await _forward(ctx, "stream.m3u8", {"src": await _checked_src(ctx, src)})


@router.get("/hls/{filename}")
async def hls_file(filename: str, request: Request, id: str | None = None) -> Response:  # noqa: A002
    require_session(request)
    ctx: AppContext = request.app.state.ctx
    if not HLS_FILE_RE.match(filename) or not id or not HLS_ID_RE.match(id):
        raise HTTPException(404)
    return await _forward(ctx, f"hls/{filename}", {"id": id})


@router.websocket("/ws")
async def ws_proxy(ws: WebSocket, src: str | None = None) -> None:
    ctx: AppContext = ws.app.state.ctx
    if not websocket_allowed(ctx, ws):
        await ws.close(code=1008)
        return
    if not src or src not in await allowed_names(ctx):
        await ws.close(code=1008)
        return
    base = ctx.settings.go2rtc_url.replace("http://", "ws://", 1)
    try:
        upstream = await websockets.connect(
            f"{base}/api/ws?{urlencode({'src': src})}", max_size=None, open_timeout=10
        )
    except (OSError, websockets.WebSocketException, TimeoutError):
        await ws.close(code=1011)
        return
    await ws.accept()
    await _pump(ws, upstream)


async def _pump(ws: WebSocket, upstream: websockets.ClientConnection) -> None:
    async def client_to_upstream() -> None:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    async def upstream_to_client() -> None:
        async for data in upstream:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_text(data)

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await upstream.close()
        with contextlib.suppress(RuntimeError, httpx.HTTPError):
            await ws.close()
