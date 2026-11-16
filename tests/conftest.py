"""Shared fixtures: a real MPEG-PS clip and a fake Hik account at the library boundary."""

from __future__ import annotations

import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from camviewer.account_state import AccountState

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@pytest.fixture(scope="session")
def ps_chunks() -> list[bytes]:
    """Two seconds of real MPEG-PS video split into network-sized chunks."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
         "-t", "2", "-c:v", "mpeg2video", "-f", "mpeg", "pipe:1"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return [out[i : i + 4096] for i in range(0, len(out), 4096)]


class FakeSession:
    """Mimics LiveStreamSession: start(), blocking iterator, close() unblocks it."""

    def __init__(self, chunks, *, result=0, raise_on_start=None, block_after=False, tail=None):
        self.chunks = chunks
        self.result = result
        self.raise_on_start = raise_on_start
        self.block_after = block_after
        self.tail = tail
        self.closed = threading.Event()

    def start(self):
        if self.raise_on_start:
            raise self.raise_on_start
        return SimpleNamespace(result=self.result)

    def iter_annex_b_chunks(self):
        for chunk in self.chunks:
            if self.closed.is_set():
                return
            yield chunk
        if self.block_after:
            self.closed.wait(timeout=30)
        if self.tail:
            raise self.tail

    def close(self):
        self.closed.set()


class FakeAccount:
    """HikAccount stand-in; `plan` is a list of FakeSession factories, last one repeats."""

    def __init__(self, tmp_path, plan):
        self.state = AccountState(tmp_path)
        self.state.mark_ok()
        self.plan = plan
        self.opened = 0
        self.relogins = 0
        self.relogin_error = None
        self.camera = SimpleNamespace(device_serial="SER1", channel_no=1, name="Cam")

    def cameras(self, force=False):
        return [self.camera]

    def open_stream(self, camera):
        factory = self.plan[min(self.opened, len(self.plan) - 1)]
        self.opened += 1
        return factory()

    def relogin(self):
        self.relogins += 1
        if self.relogin_error:
            raise self.relogin_error


class FakeHik:
    """HikAccount stand-in for API tests: accepts exactly one Hik-Connect credential pair."""

    def __init__(self, state, accept=("hikuser", "hikpw")):
        self.state = state
        self.accept = accept
        self.username = None
        self.logins = 0
        self.captcha = False
        self.online = {"SER1": True}
        self.camera = SimpleNamespace(device_serial="SER1", channel_no=1, name="Cam One")

    def login(self, username, password):
        from camviewer.hik_account import AuthError, CaptchaError

        self.logins += 1
        if self.captcha:
            self.state.mark_needs_attention("captcha")
            raise CaptchaError("captcha")
        if (username, password) != self.accept:
            raise AuthError("bad")
        self.username = username
        self.state.mark_ok()

    def cameras(self, force=False):
        return [self.camera] if self.username else []

    def online_map(self):
        return dict(self.online)

    def snapshot(self, camera):
        return b"\xff\xd8fake-jpeg"

    def relogin(self):
        pass


ORIGIN = {"origin": "http://testserver"}
APP_USER, APP_PASSWORD = "tee", "secret-pw"


@pytest.fixture(scope="session")
def app_password_hash():
    from camviewer.hash_password import hash_password

    return hash_password(APP_PASSWORD)


@pytest.fixture
def make_app(tmp_path, app_password_hash):
    """Build (app, account) with a throwaway data dir; callers wrap it in TestClient."""
    from camviewer.main import create_app
    from camviewer.settings import Settings

    def factory(**overrides):
        values = {
            "session_secret": "test-session",
            "app_username": APP_USER,
            "app_password_hash": app_password_hash,
            "hik_user": "hikuser",
            "hik_password": "hikpw",
            "cookie_secure": False,
            "data_dir": tmp_path,
            "web_dir": tmp_path / "no-web",
            "reconcile_seconds": 3600,
        } | overrides
        settings = Settings(**values)
        account = FakeHik(AccountState(tmp_path))
        return create_app(settings, account), account

    return factory


def wait_until(predicate, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def logged_in(make_app):
    """Yield (client, app, account) with a valid session cookie; Hik login already done."""
    from fastapi.testclient import TestClient

    app, account = make_app()
    with TestClient(app) as client:
        assert wait_until(lambda: account.state.usable)
        response = client.post(
            "/api/login", json={"username": APP_USER, "password": APP_PASSWORD}, headers=ORIGIN
        )
        assert response.status_code == 200
        yield client, app, account
