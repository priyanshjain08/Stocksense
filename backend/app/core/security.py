"""Password hashing (stdlib scrypt), JWT access tokens, opaque token hashing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

import jwt

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$%d$%s$%s" % (_N, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=_R, p=_P, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def create_access_token(user_id: str, token_version: int, secret: str, minutes: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "tv": token_version, "iat": now, "exp": now + minutes * 60},
                      secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


def new_opaque_token() -> tuple[str, str]:
    """Return (raw token to send to the user, sha256 hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_password_strength(pw: str) -> None:
    if len(pw) < 10:
        raise ValueError("Password must be at least 10 characters.")
