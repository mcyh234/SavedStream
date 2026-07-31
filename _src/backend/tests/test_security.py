from __future__ import annotations

from app import security
from app.security import TokenSigner, constant_time_equal, hash_secret, verify_secret


def test_constant_time_equal_compares_utf8_strings() -> None:
    assert constant_time_equal("same-密钥", "same-密钥")
    assert not constant_time_equal("same-密钥", "different")


def test_hash_secret_is_salted_and_verifiable() -> None:
    first = hash_secret("correct horse battery staple")
    second = hash_secret("correct horse battery staple")

    assert first.startswith("scrypt$")
    assert first != second
    assert verify_secret("correct horse battery staple", first)
    assert not verify_secret("wrong secret", first)


def test_verify_secret_rejects_malformed_or_unknown_hashes() -> None:
    assert not verify_secret("secret", "")
    assert not verify_secret("secret", "sha256$salt$digest")
    assert not verify_secret("secret", "scrypt$not-base64!$not-base64!")
    assert not verify_secret("secret", "scrypt$only-two-fields")


def test_token_signer_validates_kind_subject_and_signature() -> None:
    signer = TokenSigner("admin-key")
    token = signer.issue("admin", "control", ttl_seconds=60)

    assert signer.verify(token, "admin", "control")
    assert not signer.verify(token, "viewer", "control")
    assert not signer.verify(token, "admin", "other")
    assert not TokenSigner("different-key").verify(token, "admin", "control")

    body, signature = token.split(".", 1)
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = f"{body}.{signature[:-1]}{replacement}"
    assert not signer.verify(tampered, "admin", "control")


def test_token_signer_rejects_expired_and_malformed_tokens(monkeypatch) -> None:
    monkeypatch.setattr(security.time, "time", lambda: 1_000)
    signer = TokenSigner("admin-key")
    token = signer.issue("admin", "control", ttl_seconds=10)

    monkeypatch.setattr(security.time, "time", lambda: 1_011)
    assert not signer.verify(token, "admin", "control")
    assert not signer.verify(None, "admin", "control")
    assert not signer.verify("missing-separator", "admin", "control")
    assert not signer.verify("%%%.$$$", "admin", "control")
