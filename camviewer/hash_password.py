"""Password hashing for the app login (scrypt) and the CLI that prints an APP_PASSWORD_HASH.

Usage: python -m camviewer.hash_password
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys

N, R, P = 2**15, 8, 1
MAXMEM = 128 * 1024 * 1024


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, N, R, P)
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"scrypt${N}${R}${P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check; any malformed hash simply fails."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def main() -> int:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("passwords differ", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("use at least 8 characters", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
