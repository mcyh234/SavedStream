import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.media_crypto import CacheCipher, CacheDecryptionError, encrypt_for_device, parse_device_public_key


def test_cache_cipher_round_trip_and_authentication():
    cipher = CacheCipher("11" * 32)
    payload = cipher.encrypt(b"private media", b"media-key")
    assert b"private media" not in payload
    assert cipher.decrypt(payload, b"media-key") == b"private media"
    try:
        cipher.decrypt(payload, b"other-key")
    except CacheDecryptionError:
        pass
    else:
        raise AssertionError("wrong associated data must fail")


def test_legacy_plaintext_is_rejected():
    cipher = CacheCipher("22" * 32)
    try:
        cipher.decrypt(b"legacy plaintext", b"media-key")
    except CacheDecryptionError:
        pass
    else:
        raise AssertionError("plaintext cache must be rejected")


def test_device_wrapping_round_trip():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    parsed = parse_device_public_key(base64.urlsafe_b64encode(public_der).rstrip(b"=").decode())
    ciphertext, headers = encrypt_for_device(b"thumbnail", parsed.public_key, b"aad")
    wrapped = base64.urlsafe_b64decode(headers["X-SavedStream-Wrapped-Key"] + "==")
    content_key = private.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    nonce = base64.urlsafe_b64decode(headers["X-SavedStream-Nonce"] + "==")
    aad = base64.urlsafe_b64decode(headers["X-SavedStream-AAD"] + "==")
    assert AESGCM(content_key).decrypt(nonce, ciphertext, aad) == b"thumbnail"