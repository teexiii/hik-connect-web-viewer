"""Account health state and the persisted session epoch."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

AUTH_FAILURE_LIMIT = 3

NO_ACCOUNT = "no_account"
OK = "ok"
NEEDS_LOGIN = "needs_login"
CAPTCHA = "captcha"


class AccountState:
    """Tracks consecutive auth failures and the cookie-invalidating epoch."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "state.json"
        self._lock = threading.Lock()
        self.status = NO_ACCOUNT
        self.auth_failures = 0
        self._epoch = self._load_epoch()

    def _load_epoch(self) -> int:
        try:
            return int(json.loads(self._path.read_text(encoding="utf-8")).get("epoch", 1))
        except (OSError, ValueError):
            return 1

    @property
    def epoch(self) -> int:
        return self._epoch

    def bump_epoch(self) -> int:
        """Invalidate every issued session cookie."""
        with self._lock:
            self._epoch += 1
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"epoch": self._epoch}), encoding="utf-8")
            os.replace(tmp, self._path)
            return self._epoch

    @property
    def usable(self) -> bool:
        return self.status == OK

    def mark_ok(self) -> None:
        with self._lock:
            self.status = OK
            self.auth_failures = 0

    def mark_no_account(self) -> None:
        with self._lock:
            self.status = NO_ACCOUNT
            self.auth_failures = 0

    def record_auth_failure(self) -> bool:
        """Count one failure; True when the breaker tripped to needs_login."""
        with self._lock:
            self.auth_failures += 1
            if self.auth_failures >= AUTH_FAILURE_LIMIT:
                self.status = NEEDS_LOGIN
                return True
            return False

    def mark_needs_login(self) -> None:
        with self._lock:
            self.status = NEEDS_LOGIN

    def mark_captcha(self) -> None:
        with self._lock:
            self.status = CAPTCHA
