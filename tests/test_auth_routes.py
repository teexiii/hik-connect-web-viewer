from fastapi.testclient import TestClient

from camviewer.credential_store import CredentialStore
from camviewer.session_auth import COOKIE_NAME
from tests.conftest import ORIGIN

GOOD = {"username": "tee", "password": "secret-pw"}
PROTECTED = [
    "/api/cameras",
    "/api/status",
    "/api/cameras/SER1_1/thumbnail",
    "/api/clips/" + "a" * 32,
    "/go2rtc/api/frame.jpeg?src=SER1_1",
    "/go2rtc/api/stream.m3u8?src=SER1_1",
    "/go2rtc/api/hls/segment.ts?id=abc",
]


def test_everything_requires_a_session(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        for path in PROTECTED:
            assert client.get(path).status_code == 401, path
        assert client.post("/api/streams/SER1_1/reconnect", headers=ORIGIN).status_code == 401
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/session").json()["authenticated"] is False


def test_wrong_password_is_401_without_cookie(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        response = client.post("/api/login", json={**GOOD, "password": "nope"}, headers=ORIGIN)
        assert response.status_code == 401
        assert COOKIE_NAME not in response.cookies


def test_login_sets_strict_httponly_cookie_and_unlocks_api(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        response = client.post("/api/login", json=GOOD, headers=ORIGIN)
        header = response.headers["set-cookie"].lower()
        assert response.status_code == 200
        assert "httponly" in header and "samesite=strict" in header
        assert client.get("/api/cameras").status_code == 200
        assert client.get("/api/session").json()["authenticated"] is True


def test_post_requires_matching_origin(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        assert client.post("/api/login", json=GOOD).status_code == 403
        evil = {"origin": "http://evil.example"}
        assert client.post("/api/login", json=GOOD, headers=evil).status_code == 403


def test_login_always_hits_the_cloud_and_stores_encrypted(make_app, tmp_path):
    app, account = make_app()
    with TestClient(app) as client:
        client.post("/api/login", json=GOOD, headers=ORIGIN)
        client.post("/api/login", json=GOOD, headers=ORIGIN)
    assert account.logins == 2
    assert b"secret-pw" not in (tmp_path / "credentials.enc").read_bytes()
    assert CredentialStore(tmp_path, "test-secret").load() == ("tee", "secret-pw")


def test_other_account_is_409_without_a_session(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        client.post("/api/login", json=GOOD, headers=ORIGIN)
        client.cookies.clear()
        other = {"username": "someone", "password": "x"}
        assert client.post("/api/login", json=other, headers=ORIGIN).status_code == 409


def test_captcha_is_423(make_app):
    app, account = make_app()
    account.captcha = True
    with TestClient(app) as client:
        assert client.post("/api/login", json=GOOD, headers=ORIGIN).status_code == 423


def test_login_rate_limit_is_global(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        bad = {**GOOD, "password": "x"}
        codes = [client.post("/api/login", json=bad, headers=ORIGIN).status_code for _ in range(11)]
        assert codes[:10] == [401] * 10 and codes[10] == 429


def test_logout_revokes_old_cookie(logged_in):
    client, _, _ = logged_in
    old = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/logout", headers=ORIGIN).status_code == 200
    assert client.get("/api/cameras").status_code == 401
    client.cookies.set(COOKIE_NAME, old)
    assert client.get("/api/cameras").status_code == 401


def test_forget_account_removes_credentials(logged_in, tmp_path):
    client, _, account = logged_in
    assert client.post("/api/account/forget", headers=ORIGIN).status_code == 200
    assert not (tmp_path / "credentials.enc").exists()
    assert account.username is None
    assert client.get("/api/cameras").status_code == 401


def test_tampered_cookie_rejected(logged_in):
    client, _, _ = logged_in
    client.cookies.set(COOKIE_NAME, "garbage.value.here")
    assert client.get("/api/cameras").status_code == 401
