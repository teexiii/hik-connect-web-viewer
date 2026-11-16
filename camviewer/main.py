"""Entry point: public app (auth, API, proxy, static) + loopback internal app, one process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from camviewer import auth_routes, camera_routes, go2rtc_proxy_routes
from camviewer.app_context import AppContext, build_context
from camviewer.hik_account import HikAccount
from camviewer.hik_startup_login import restart_login
from camviewer.internal_routes import create_internal_app
from camviewer.live_ingest_manager import stream_id_for
from camviewer.session_auth import SlidingSessionMiddleware
from camviewer.settings import Settings

log = logging.getLogger("camviewer")


async def maintenance_loop(ctx: AppContext) -> None:
    """Keep go2rtc registered (it forgets streams on restart) and prune old clips."""
    while True:
        try:
            if ctx.state.usable:
                cameras = await asyncio.to_thread(ctx.account.cameras)
                await ctx.go2rtc.reconcile([stream_id_for(c) for c in cameras])
            await asyncio.to_thread(ctx.clips.cleanup)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.warning("maintenance error: %s", type(exc).__name__)
        await asyncio.sleep(ctx.settings.reconcile_seconds)


def create_app(settings: Settings, account: HikAccount | None = None) -> FastAPI:
    ctx = build_context(settings, account)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        restart_login(ctx)
        maintenance = asyncio.create_task(maintenance_loop(ctx))
        try:
            yield
        finally:
            maintenance.cancel()
            if ctx.login_task:
                ctx.login_task.cancel()
            await asyncio.to_thread(ctx.manager.stop_all)
            await ctx.http.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(SlidingSessionMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(auth_routes.router)
    app.include_router(camera_routes.router)
    app.include_router(go2rtc_proxy_routes.router)
    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


async def serve(settings: Settings) -> None:
    public_app = create_app(settings)
    internal_app = create_internal_app(public_app)
    public = uvicorn.Server(
        uvicorn.Config(
            public_app,
            host=settings.public_host,
            port=settings.public_port,
            proxy_headers=True,
            forwarded_allow_ips=settings.forwarded_allow_ips,
        )
    )
    internal = uvicorn.Server(
        uvicorn.Config(internal_app, host="127.0.0.1", port=settings.internal_port)
    )
    internal.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    async def stop_internal_after_public() -> None:
        await public_task
        internal.should_exit = True

    public_task = asyncio.create_task(public.serve())
    await asyncio.gather(internal.serve(), stop_internal_after_public())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry tokens
    settings = Settings.from_env()
    settings.validate()
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
