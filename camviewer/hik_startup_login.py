"""Background Hik-Connect login from env credentials: non-fatal, backs off, stops on lockout."""

from __future__ import annotations

import asyncio
import logging

from camviewer.app_context import AppContext
from camviewer.hik_account import AuthError, CaptchaError

log = logging.getLogger("camviewer.login")
FIRST_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0


async def startup_login(ctx: AppContext) -> None:
    """Retry network failures forever; stop on CAPTCHA or three auth failures."""
    settings = ctx.settings
    if not (settings.hik_user and settings.hik_password):
        ctx.state.mark_needs_attention("config")
        log.error("HIK_CONNECT_USER / HIK_CONNECT_PASSWORD are not set")
        return
    ctx.state.mark_connecting()
    delay = FIRST_RETRY_SECONDS
    while True:
        try:
            await asyncio.to_thread(ctx.account.login, settings.hik_user, settings.hik_password)
            log.info("hik-connect login ok")
            return
        except CaptchaError:
            log.warning("hik-connect needs CAPTCHA; log in once in the official app")
            return
        except AuthError:
            if ctx.state.record_auth_failure():
                log.warning("hik-connect rejected the credentials 3 times; giving up")
                return
        except Exception as exc:  # noqa: BLE001 - network down: keep trying
            log.warning("hik-connect login failed (%s); retrying", type(exc).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RETRY_SECONDS)


def restart_login(ctx: AppContext) -> None:
    """Cancel a running attempt (if any) and start a fresh one."""
    if ctx.login_task and not ctx.login_task.done():
        ctx.login_task.cancel()
    ctx.login_task = asyncio.create_task(startup_login(ctx))
