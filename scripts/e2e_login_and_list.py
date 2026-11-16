"""Local E2E helper: log in to the app (own login) and list cameras.

Usage: uv run python scripts/e2e_login_and_list.py APP_LOGIN_JSON BASE_URL COOKIE_OUT
APP_LOGIN_JSON holds {"username": ..., "password": ...}. Nothing secret is printed; the session
cookie is saved to COOKIE_OUT for the browser test.
"""

import json
import sys
from pathlib import Path

import httpx

login_file, base, cookie_out = sys.argv[1:4]
with httpx.Client(base_url=base, headers={"origin": base}) as client:
    r = client.post("/api/login", json=json.loads(Path(login_file).read_text(encoding="utf-8")))
    print("login", r.status_code)
    if r.status_code != 200:
        sys.exit(1)
    print(client.get("/api/status").json())
    print(json.dumps(client.get("/api/cameras").json(), ensure_ascii=False))
    Path(cookie_out).write_text(json.dumps(dict(client.cookies)), encoding="utf-8")
