from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        secret.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_secret(secret: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, digest_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.scrypt(
            secret.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class TokenSigner:
    def __init__(self, secret: str) -> None:
        self._secret = hashlib.sha256(secret.encode("utf-8")).digest()

    def issue(self, kind: str, subject: str, ttl_seconds: int) -> str:
        payload = {
            "kind": kind,
            "sub": subject,
            "exp": int(time.time()) + ttl_seconds,
            "nonce": secrets.token_urlsafe(8),
        }
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{body.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def verify(self, token: str | None, kind: str, subject: str) -> bool:
        if not token:
            return False
        try:
            body_text, signature_text = token.split(".", 1)
            body = body_text.encode("ascii")
            signature = _decode_base64(signature_text)
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload = json.loads(_decode_base64(body_text))
            return (
                payload.get("kind") == kind
                and payload.get("sub") == subject
                and int(payload.get("exp", 0)) >= int(time.time())
            )
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return False


def _decode_base64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))

