"""Loopback-only app: MPEG-TS source that go2rtc pulls, plus the health probe."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from camviewer.h264_transcode import transcode
from camviewer.live_ingest_manager import TooManyStreams, UnknownStream


def create_internal_app(ctx_holder: FastAPI) -> FastAPI:
    """`ctx_holder` is the public app; the context is read lazily from its state."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    async def ensure_stream(stream_id: str):  # noqa: ANN202
        ctx = ctx_holder.state.ctx
        if not ctx.state.usable:
            raise HTTPException(503, "account not ready")
        try:
            await ctx.manager.ensure(stream_id)
        except UnknownStream as exc:
            raise HTTPException(404) from exc
        except TooManyStreams as exc:
            raise HTTPException(429, "too many live streams") from exc
        return ctx

    @app.get("/internal/ts/{stream_id}")
    async def ts_source(stream_id: str, request: Request) -> StreamingResponse:
        ctx = await ensure_stream(stream_id)

        async def body() -> AsyncIterator[bytes]:
            source = ctx.manager.subscribe(stream_id)
            try:
                async for chunk in source:
                    yield chunk
            finally:
                await source.aclose()

        return StreamingResponse(body(), media_type="video/mp2t")

    @app.get("/internal/h264/{stream_id}")
    async def h264_source(stream_id: str) -> StreamingResponse:
        """Same camera, transcoded to H.264 by the app's ffmpeg (see h264_transcode)."""
        ctx = await ensure_stream(stream_id)
        source = ctx.manager.subscribe(stream_id)
        body = transcode(source, ctx.manager.ffmpeg_path)
        return StreamingResponse(body, media_type="video/mp2t")

    return app
