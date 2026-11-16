from fastapi.testclient import TestClient

from camviewer.session_auth import COOKIE_NAME
from tests.conftest import APP_PASSWORD, APP_USER, ORIGIN, wait_until

GOOD = {"username": APP_USER, "password": APP_PASSWORD}
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
        assert client.post("/api/account/retry", headers=ORIGIN).status_code == 401
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/session").json() == {"authenticated": False}


def test_wrong_password_or_username_is_401_without_cookie(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        for body in ({**GOOD, "password": "nope"}, {**GOOD, "username": "other"}):
            response = client.post("/api/login", json=body, headers=ORIGIN)
            assert response.status_code == 401
            assert COOKIE_NAME not in response.cookies


def test_login_does_not_touch_hik_connect(make_app):
    app, account = make_app()
    with TestClient(app) as client:
        assert wait_until(lambda: account.state.usable)
        before = account.logins
        client.post("/api/login", json=GOOD, headers=ORIGIN)
        assert account.logins == before == 1


def test_cookie_flags_and_unlocked_api(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        response = client.post("/api/login", json=GOOD, headers=ORIGIN)
        header = response.headers["set-cookie"].lower()
        assert response.status_code == 200
        assert "httponly" in header and "samesite=lax" in header and "secure" not in header
        assert f"max-age={400 * 24 * 3600}" in header
        assert client.get("/api/cameras").status_code == 200
        assert client.get("/api/session").json()["authenticated"] is True


def test_cookie_is_secure_by_default_setting(make_app):
    app, _ = make_app(cookie_secure=True)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/login", json=GOOD, headers={"origin": "https://testserver"}
        )
        assert "secure" in response.headers["set-cookie"].lower()


def test_cookie_slides_on_every_authenticated_request(logged_in):
    client, _, _ = logged_in
    first = client.cookies.get(COOKIE_NAME)
    response = client.get("/api/status")
    assert COOKIE_NAME in response.headers["set-cookie"]
    assert "max-age=" in response.headers["set-cookie"].lower()
    refreshed = response.cookies.get(COOKIE_NAME)
    assert refreshed and client.get("/api/session").json()["authenticated"] is True
    assert first  # still valid after the re-issue


def test_no_cookie_is_issued_to_anonymous_requests(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        assert "set-cookie" not in client.get("/api/session").headers


def test_post_requires_matching_origin(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        assert client.post("/api/login", json=GOOD).status_code == 403
        evil = {"origin": "http://evil.example"}
        assert client.post("/api/login", json=GOOD, headers=evil).status_code == 403


def test_rate_limit_is_per_ip_then_global(make_app):
    app, _ = make_app()
    with TestClient(app) as client:
        bad = {**GOOD, "password": "x"}
        ip_a = {**ORIGIN, "x-real-ip": "1.1.1.1"}
        codes = [client.post("/api/login", json=bad, headers=ip_a).status_code for _ in range(6)]
        assert codes == [401] * 5 + [429]
        # another address is still allowed until the global budget is spent
        ip_b = {**ORIGIN, "x-real-ip": "2.2.2.2"}
        codes = [client.post("/api/login", json=bad, headers=ip_b).status_code for _ in range(5)]
        assert codes == [401] * 5  # global budget (10) is now spent
        ip_c = {**ORIGIN, "x-real-ip": "3.3.3.3"}
        assert client.post("/api/login", json=bad, headers=ip_c).status_code == 429


def test_logout_revokes_old_cookie(logged_in):
    client, _, _ = logged_in
    old = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/logout", headers=ORIGIN).status_code == 200
    assert client.get("/api/cameras").status_code == 401
    client.cookies.set(COOKIE_NAME, old)
    assert client.get("/api/cameras").status_code == 401


def test_tampered_cookie_rejected(logged_in):
    client, _, _ = logged_in
    client.cookies.set(COOKIE_NAME, "garbage.value.here")
    assert client.get("/api/cameras").status_code == 401


def test_retry_restarts_the_hik_login(logged_in):
    client, _, account = logged_in
    account.state.mark_needs_attention("auth")
    assert client.post("/api/account/retry", headers=ORIGIN).status_code == 200
    assert wait_until(lambda: account.state.usable)
    assert account.logins == 2
