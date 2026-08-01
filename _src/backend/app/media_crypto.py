from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CACHE_MAGIC = b"SSCACHE\x01"
NONCE_BYTES = 12

class CacheDecryptionError(ValueError):
    pass

class DeviceKeyError(ValueError):
    pass

def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

def _b64d(value: str) -> bytes:
    clean = value.strip()
    return base64.urlsafe_b64decode((clean + "=" * (-len(clean) % 4)).encode("ascii"))

def decode_media_cache_key(value: str) -> bytes:
    clean = value.strip()
    if not clean:
        raise ValueError("MEDIA_CACHE_KEY is required")
    try:
        raw = bytes.fromhex(clean) if len(clean) == 64 else _b64d(clean)
    except ValueError as exc:
        raise ValueError("MEDIA_CACHE_KEY must be 32-byte hex or base64") from exc
    if len(raw) != 32:
        raise ValueError("MEDIA_CACHE_KEY must decode to exactly 32 bytes")
    return raw

class CacheCipher:
    def __init__(self, key_text: str) -> None:
        self._cipher = AESGCM(decode_media_cache_key(key_text))

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return CACHE_MAGIC + nonce + self._cipher.encrypt(nonce, plaintext, associated_data)

    def decrypt(self, payload: bytes, associated_data: bytes) -> bytes:
        if not payload.startswith(CACHE_MAGIC):
            raise CacheDecryptionError("Legacy plaintext cache entry")
        start = len(CACHE_MAGIC)
        end = start + NONCE_BYTES
        if len(payload) < end + 16:
            raise CacheDecryptionError("Encrypted cache entry is truncated")
        try:
            return self._cipher.decrypt(payload[start:end], payload[end:], associated_data)
        except InvalidTag as exc:
            raise CacheDecryptionError("Encrypted cache authentication failed") from exc

@dataclass(frozen=True)
class ParsedDeviceKey:
    public_key_pem: str
    fingerprint: str
    public_key: rsa.RSAPublicKey

def parse_device_public_key(encoded_spki: str) -> ParsedDeviceKey:
    try:
        der = _b64d(encoded_spki)
        key = serialization.load_der_public_key(der)
    except (ValueError, TypeError) as exc:
        raise DeviceKeyError("Device public key is not valid SPKI data") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise DeviceKeyError("Device key must be RSA with at least 2048 bits")
    canonical = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pem = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return ParsedDeviceKey(pem, hashlib.sha256(canonical).hexdigest(), key)

def load_device_public_key(public_key_pem: str) -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise DeviceKeyError("Stored device key is invalid")
    return key

def encrypt_for_device(plaintext: bytes, public_key: rsa.RSAPublicKey, associated_data: bytes) -> tuple[bytes, dict[str, str]]:
    content_key = os.urandom(32)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(content_key).encrypt(nonce, plaintext, associated_data)
    wrapped = public_key.encrypt(content_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return ciphertext, {
        "X-SavedStream-Crypto-Version": "1",
        "X-SavedStream-Key-Algorithm": "RSA-OAEP-256+A256GCM",
        "X-SavedStream-Wrapped-Key": _b64e(wrapped),
        "X-SavedStream-Nonce": _b64e(nonce),
        "X-SavedStream-AAD": _b64e(associated_data),
        "X-SavedStream-Plaintext-Length": str(len(plaintext)),
    }
