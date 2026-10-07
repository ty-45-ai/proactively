"""Passcode hashing, login throttling and session cookies."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

PASSCODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i
COOKIE_NAME = "ondoku_session"
PARTICIPANT_TTL = 60 * 60 * 24 * 60  # 60 days: children should not need to log in every time
ADMIN_TTL = 60 * 60 * 12


def hash_secret(secret: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_passcode(length: int = 6) -> str:
    return "".join(secrets.choice(PASSCODE_ALPHABET) for _ in range(length))


def new_token() -> str:
    return secrets.token_urlsafe(32)


class LoginThrottle:
    """Lock a key (client IP + login name) for `lock_seconds` after `max_failures` misses."""

    def __init__(self, max_failures: int = 8, lock_seconds: int = 300):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._failures.get(key, []) if now - t < self.lock_seconds]

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= self.max_failures

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._failures[key] = self._recent(key, now) + [now]

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
