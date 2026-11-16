from __future__ import annotations

import pytest
from hikcloudstream import AuthenticationError, CaptchaRequiredError, StreamType

from camviewer import hik_account
from camviewer.account_state import AccountState
from camviewer.hik_account import AuthError, CaptchaError, HikAccount, classify


class FakeClient:
    def __init__(self, login_error=None):
        self.login_error = login_error
        self.logins = 0
        self.list_calls = 0
        self.closed = False

    def login(self, creds):
        self.logins += 1
        if self.login_error:
            raise self.login_error

    def list_cameras(self):
        self.list_calls += 1
        return ["cam"]

    def close(self):
        self.closed = True


@pytest.fixture
def state(tmp_path):
    return AccountState(tmp_path)


def test_login_success_marks_ok(state):
    account = HikAccount(state, client_factory=FakeClient)
    account.login("u", "p")
    assert state.status == "ok" and account.username == "u"


def test_login_bad_password_raises_and_keeps_state(state):
    account = HikAccount(state, client_factory=lambda: FakeClient(AuthenticationError("bad")))
    with pytest.raises(AuthError):
        account.login("u", "p")
    assert state.status == "no_account"


def test_login_captcha(state):
    account = HikAccount(state, client_factory=lambda: FakeClient(CaptchaRequiredError("c")))
    with pytest.raises(CaptchaError):
        account.login("u", "p")
    assert state.status == "captcha"


def test_cameras_cached_until_forced(state):
    clients = []

    def factory():
        clients.append(FakeClient())
        return clients[-1]

    account = HikAccount(state, client_factory=factory)
    account.login("u", "p")
    account.cameras()
    account.cameras()
    assert clients[-1].list_calls == 1
    account.cameras(force=True)
    assert clients[-1].list_calls == 2


def test_relogin_failures_trip_breaker(state, monkeypatch):
    monkeypatch.setattr(hik_account, "RELOGIN_MIN_INTERVAL", 0.0)
    good = HikAccount(state, client_factory=FakeClient)
    good.login("u", "p")
    good._factory = lambda: FakeClient(AuthenticationError("changed password"))
    for _ in range(3):
        with pytest.raises(AuthError):
            good.relogin()
    assert state.status == "needs_login"
    with pytest.raises(AuthError):
        good.relogin()


def test_relogin_is_rate_limited(state):
    account = HikAccount(state, client_factory=FakeClient)
    account.login("u", "p")
    account.relogin()  # first one is allowed
    with pytest.raises(AuthError, match="rate"):
        account.relogin()


def test_open_stream_uses_main_stream_type(state):
    seen = {}

    def opener(client, camera, **kwargs):
        seen.update(kwargs)
        return "session"

    account = HikAccount(state, client_factory=FakeClient, opener=opener)
    account.login("u", "p")
    assert account.open_stream("cam") == "session"
    assert seen["stream_type"] == StreamType.MAIN


def test_forget_clears_everything(state):
    account = HikAccount(state, client_factory=FakeClient)
    account.login("u", "p")
    account.forget()
    assert account.cameras() == [] and account.username is None
    assert state.status == "no_account"


def test_classify():
    from pyezvizapi.exceptions import DeviceException

    assert classify(CaptchaRequiredError("x")) == "captcha"
    assert classify(AuthenticationError("x")) == "auth"
    assert classify(DeviceException("x")) == "offline"
    assert classify(RuntimeError("x")) == "other"
