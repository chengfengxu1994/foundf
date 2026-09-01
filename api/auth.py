"""Small signed-cookie authentication boundary for the customer dashboard."""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any


PBKDF2_ITERATIONS = 310_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Create a salted PBKDF2 password hash suitable for an environment value."""

    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = _b64decode(raw_salt)
        expected = _b64decode(raw_digest)
    except (TypeError, ValueError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


@dataclass
class AuthManager:
    enabled: bool = False
    username: str = ""
    password_hash: str = ""
    secret_key: str = ""
    session_seconds: int = 8 * 3600
    cookie_name: str = "foundf_session"
    secure_cookie: bool = False
    max_failures: int = 5
    failure_window_seconds: int = 300
    _failures: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "AuthManager":
        enabled = os.getenv("DASHBOARD_AUTH_ENABLED", "false").lower() in {
            "1", "true", "yes", "on"
        }
        manager = cls(
            enabled=enabled,
            username=os.getenv("DASHBOARD_USERNAME", ""),
            password_hash=os.getenv("DASHBOARD_PASSWORD_HASH", ""),
            secret_key=os.getenv("DASHBOARD_SECRET_KEY", ""),
            session_seconds=int(os.getenv("DASHBOARD_SESSION_SECONDS", "28800")),
            secure_cookie=os.getenv(
                "DASHBOARD_COOKIE_SECURE", "false"
            ).lower() in {"1", "true", "yes", "on"},
        )
        if enabled and (
            not manager.username
            or not manager.password_hash
            or len(manager.secret_key) < 32
        ):
            raise RuntimeError(
                "dashboard auth requires DASHBOARD_USERNAME, "
                "DASHBOARD_PASSWORD_HASH and a 32+ character DASHBOARD_SECRET_KEY"
            )
        return manager

    def _signature(self, payload: str) -> str:
        return _b64encode(
            hmac.new(
                self.secret_key.encode("utf-8"),
                payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )

    def issue_session(self, subject: str | None = None, now: int | None = None) -> str:
        now = int(time.time()) if now is None else int(now)
        payload = _b64encode(
            json.dumps(
                {
                    "sub": subject or self.username,
                    "iat": now,
                    "exp": now + self.session_seconds,
                    "nonce": secrets.token_hex(8),
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return f"{payload}.{self._signature(payload)}"

    def verify_session(self, token: str, now: int | None = None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._signature(payload)):
            return None
        try:
            claims = json.loads(_b64decode(payload))
            expires_at = int(claims.get("exp", 0))
        except (TypeError, ValueError, json.JSONDecodeError, binascii.Error):
            return None
        now = int(time.time()) if now is None else int(now)
        if claims.get("sub") != self.username or expires_at <= now:
            return None
        return claims

    def _recent_failures(self, client_key: str, now: float) -> list[float]:
        cutoff = now - self.failure_window_seconds
        recent = [stamp for stamp in self._failures.get(client_key, []) if stamp >= cutoff]
        self._failures[client_key] = recent
        return recent

    def authenticate(
        self, username: str, password: str, client_key: str = "unknown"
    ) -> tuple[bool, str]:
        now = time.time()
        failures = self._recent_failures(client_key, now)
        if len(failures) >= self.max_failures:
            return False, "RATE_LIMITED"
        username_valid = hmac.compare_digest(username, self.username)
        password_valid = verify_password(password, self.password_hash)
        valid = username_valid and password_valid
        if not valid:
            failures.append(now)
            self._failures[client_key] = failures
            return False, "INVALID_CREDENTIALS"
        self._failures.pop(client_key, None)
        return True, "OK"


def _main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "hash-password":
        print("Usage: python -m api.auth hash-password", file=sys.stderr)
        return 2
    first = getpass.getpass("Dashboard password (10+ characters): ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        print(hash_password(first))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
