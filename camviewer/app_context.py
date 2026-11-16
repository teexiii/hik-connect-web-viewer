"""Wiring container shared by the public and internal apps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from camviewer.account_state import AccountState
from camviewer.clip_recorder import ClipRecorder
from camviewer.go2rtc_client import Go2RtcClient
from camviewer.hik_account import HikAccount
from camviewer.live_ingest_manager import LiveIngestManager
from camviewer.session_auth import LoginRateLimiter, SessionSigner
from camviewer.settings import Settings


@dataclass
class AppContext:
    settings: Settings
    state: AccountState
    account: HikAccount
    manager: LiveIngestManager
    signer: SessionSigner
    limiter: LoginRateLimiter
    go2rtc: Go2RtcClient
    clips: ClipRecorder
    http: httpx.AsyncClient
    login_task: asyncio.Task[None] | None = None


def build_context(settings: Settings, account: HikAccount | None = None) -> AppContext:
    state = AccountState(settings.data_dir)
    account = account or HikAccount(state)
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=None))
    return AppContext(
        settings=settings,
        state=account.state,
        account=account,
        manager=LiveIngestManager(
            account,
            max_streams=settings.max_streams,
            idle_stop_seconds=settings.idle_stop_seconds,
            stall_seconds=settings.stall_seconds,
            rotate_seconds=settings.session_rotate_seconds,
        ),
        signer=SessionSigner(settings.session_secret),
        limiter=LoginRateLimiter(),
        go2rtc=Go2RtcClient(settings.go2rtc_url, settings.internal_ts_base, http),
        clips=ClipRecorder(settings.data_dir, settings.internal_ts_base),
        http=http,
    )
