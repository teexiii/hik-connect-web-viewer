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
from camviewer.hik_account import AuthError, CaptchaError, HikAccount
from camviewer.internal_routes import create_internal_app
from camviewer.live_ingest_manager import stream_id_for
from camviewer.settings import Settings

log = logging.getLogger("camviewer")
STARTUP_RETRY_SECONDS = 30


async def startup_login(ctx: AppContext) -> None:
    """Log in with stored credentials; never crash, never hammer a rejecting cloud."""
    creds = await asyncio.to_thread(ctx.store.load)
    if creds is None:
        return
    while True:
        try:
            await asyncio.to_thread(ctx.account.login, *creds)
            log.info("startup login ok")
            return
        except CaptchaError:
            log.warning("startup login needs CAPTCHA")
            return
        except AuthError:
            ctx.state.mark_needs_login()
            log.warning("stored credentials rejected; waiting for a new login")
            return
        except Exception as exc:  # noqa: BLE001 - network down: retry later
            log.warning("startup login failed (%s); retrying", type(exc).__name__)
            await asyncio.sleep(STARTUP_RETRY_SECONDS)


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
        tasks = [
            asyncio.create_task(startup_login(ctx)),
            asyncio.create_task(maintenance_loop(ctx)),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.to_thread(ctx.manager.stop_all)
            await ctx.http.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx

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
        uvicorn.Config(public_app, host=settings.public_host, port=settings.public_port)
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
    settings = Settings.from_env()
    settings.validate()
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
