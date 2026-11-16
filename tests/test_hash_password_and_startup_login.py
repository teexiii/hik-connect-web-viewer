import asyncio

from camviewer.account_state import AccountState
from camviewer.app_context import build_context
from camviewer.hash_password import hash_password, verify_password
from camviewer.hik_startup_login import startup_login
from camviewer.settings import Settings
from tests.conftest import FakeHik


def test_hash_roundtrip_and_rejects_malformed():
    encoded = hash_password("correct horse")
    assert encoded.startswith("scrypt$")
    assert verify_password("correct horse", encoded)
    assert not verify_password("wrong", encoded)
    for junk in ("", "plain", "scrypt$1$2", "bcrypt$a$b$c$d$e", "scrypt$x$8$1$AA==$AA=="):
        assert not verify_password("correct horse", junk)


def test_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


def _ctx(tmp_path, **kw):
    settings = Settings(
        session_secret="s", data_dir=tmp_path, hik_user="hikuser", hik_password="hikpw", **kw
    )
    return build_context(settings, FakeHik(AccountState(tmp_path)))


async def test_missing_hik_env_needs_attention_without_crashing(tmp_path):
    ctx = build_context(
        Settings(session_secret="s", data_dir=tmp_path), FakeHik(AccountState(tmp_path))
    )
    await startup_login(ctx)
    assert ctx.state.status == "needs_attention" and ctx.state.reason == "config"


async def test_login_succeeds(tmp_path):
    ctx = _ctx(tmp_path)
    await startup_login(ctx)
    assert ctx.state.usable


async def test_three_auth_failures_stop_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr("camviewer.hik_startup_login.FIRST_RETRY_SECONDS", 0.01)
    ctx = _ctx(tmp_path)
    ctx.account.accept = ("other", "creds")
    await asyncio.wait_for(startup_login(ctx), 5)
    assert ctx.account.logins == 3
    assert ctx.state.status == "needs_attention" and ctx.state.reason == "auth"


async def test_captcha_stops_at_once(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.account.captcha = True
    await startup_login(ctx)
    assert ctx.account.logins == 1
    assert ctx.state.reason == "captcha"


async def test_network_errors_are_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("camviewer.hik_startup_login.FIRST_RETRY_SECONDS", 0.01)
    ctx = _ctx(tmp_path)
    original = ctx.account.login
    calls = []

    def flaky(user, password):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("network down")
        original(user, password)

    ctx.account.login = flaky
    await asyncio.wait_for(startup_login(ctx), 5)
    assert len(calls) == 3 and ctx.state.usable

