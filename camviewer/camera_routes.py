"""Camera list, status, thumbnails, reconnect, snapshot and clip endpoints."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from camviewer.app_context import AppContext
from camviewer.clip_recorder import ClipBusy
from camviewer.live_ingest_manager import UnknownStream, stream_id_for
from camviewer.session_auth import require_post_origin, require_session

router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])
THUMB_TTL_SECONDS = 60
_thumb_cache: dict[str, tuple[float, bytes]] = {}


class ClipBody(BaseModel):
    seconds: int = 30


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _filename(name: str, ext: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "camera"
    return f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"


async def _camera(ctx: AppContext, stream_id: str) -> Any:
    try:
        return await ctx.manager.find_camera(stream_id)
    except UnknownStream as exc:
        raise HTTPException(404, "unknown camera") from exc


def _online(known: bool | None, health: dict[str, Any] | None) -> bool | None:
    """Cloud device status wins; a stream that reports offline also marks it offline."""
    if health and health["state"] == "offline":
        return False
    return known


@router.get("/cameras")
async def list_cameras(request: Request) -> list[dict[str, Any]]:
    ctx = _ctx(request)
    cameras = await asyncio.to_thread(ctx.account.cameras)
    online = await asyncio.to_thread(ctx.account.online_map)
    status = ctx.manager.status()
    result = []
    for camera in cameras:
        sid = stream_id_for(camera)
        result.append(
            {
                "id": sid,
                "name": camera.name,
                "serial": camera.device_serial,
                "channel": camera.channel_no,
                "online": _online(online.get(camera.device_serial), status.get(sid)),
                "streams": {"hevc": sid, "h264": f"{sid}_h264"},
            }
        )
    return result


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    return {
        "account": {"status": ctx.state.status, "reason": ctx.state.reason},
        "streams": ctx.manager.status(),
    }


@router.post("/streams/{stream_id}/reconnect", dependencies=[Depends(require_post_origin)])
async def reconnect(stream_id: str, request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    await _camera(ctx, stream_id)
    return {"ok": True, "active": ctx.manager.force_reconnect(stream_id)}


@router.get("/cameras/{stream_id}/thumbnail")
async def thumbnail(stream_id: str, request: Request) -> Response:
    ctx = _ctx(request)
    camera = await _camera(ctx, stream_id)
    cached = _thumb_cache.get(stream_id)
    if cached and time.monotonic() - cached[0] < THUMB_TTL_SECONDS:
        return Response(cached[1], media_type="image/jpeg")
    try:
        jpeg = await asyncio.to_thread(ctx.account.snapshot, camera)
    except Exception as exc:  # noqa: BLE001 - offline camera / cloud error
        raise HTTPException(502, "snapshot unavailable") from exc
    _thumb_cache[stream_id] = (time.monotonic(), jpeg)
    return Response(jpeg, media_type="image/jpeg")


@router.get("/cameras/{stream_id}/snapshot")
async def snapshot(stream_id: str, request: Request) -> Response:
    ctx = _ctx(request)
    camera = await _camera(ctx, stream_id)
    upstream = await ctx.http.get(
        f"{ctx.settings.go2rtc_url}/api/frame.jpeg",
        params={"src": f"{stream_id}_h264"},
        timeout=30,
    )
    if not upstream.is_success or not upstream.content:
        raise HTTPException(502, "no frame available")
    disposition = f'attachment; filename="{_filename(camera.name, "jpg")}"'
    return Response(
        upstream.content,
        media_type="image/jpeg",
        headers={"Content-Disposition": disposition},
    )


@router.post("/cameras/{stream_id}/clip", dependencies=[Depends(require_post_origin)])
async def start_clip(stream_id: str, body: ClipBody, request: Request) -> dict[str, str]:
    ctx = _ctx(request)
    await _camera(ctx, stream_id)
    try:
        job_id = ctx.clips.start(stream_id, body.seconds)
    except ValueError as exc:
        raise HTTPException(422, "seconds must be between 10 and 120") from exc
    except ClipBusy as exc:
        raise HTTPException(429, "too many clips recording") from exc
    return {"job_id": job_id}


@router.get("/clips/{job_id}")
async def clip_status(job_id: str, request: Request) -> dict[str, Any]:
    job = _ctx(request).clips.status(job_id)
    if job is None:
        raise HTTPException(404, "unknown clip")
    return job


@router.get("/clips/{job_id}/download")
async def clip_download(job_id: str, request: Request) -> FileResponse:
    path = _ctx(request).clips.path_for(job_id)
    if path is None:
        raise HTTPException(404, "clip not ready")
    return FileResponse(path, media_type="video/mp4", filename=_filename("clip", "mp4"))
