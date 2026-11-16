"""Fernet-encrypted Hik-Connect credential file (atomic write, chmod 600)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _derive_key(secret: str) -> bytes:
    """Accept a native Fernet key, or derive one from any passphrase."""
    try:
        Fernet(secret.encode())
        return secret.encode()
    except ValueError:
        return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


class CredentialStore:
    def __init__(self, data_dir: Path, secret: str) -> None:
        self._path = data_dir / "credentials.enc"
        self._fernet = Fernet(_derive_key(secret))

    def save(self, username: str, password: str) -> None:
        blob = self._fernet.encrypt(json.dumps({"u": username, "p": password}).encode())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        with contextlib.suppress(OSError):  # permission bits are best-effort on Windows
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def load(self) -> tuple[str, str] | None:
        try:
            data = json.loads(self._fernet.decrypt(self._path.read_bytes()))
        except (OSError, InvalidToken, ValueError):
            return None
        return data["u"], data["p"]

    def username(self) -> str | None:
        creds = self.load()
        return creds[0] if creds else None

    def forget(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
