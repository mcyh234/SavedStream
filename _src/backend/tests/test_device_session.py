import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.database import Database
from app.main import registered_device_public_key, signer
from app.media_crypto import parse_device_public_key


def encoded_public_key() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.urlsafe_b64encode(public_der).rstrip(b"=").decode("ascii")


@pytest.mark.asyncio
async def test_device_key_requires_matching_session_and_honors_revocation(tmp_path) -> None:
    database = Database(tmp_path / "device-session.db")
    await database.initialize()
    parsed = parse_device_public_key(encoded_public_key())
    assert await database.register_device_key(parsed.fingerprint, parsed.public_key_pem)
    cookie = signer.issue("device", parsed.fingerprint, 60)

    key = await registered_device_public_key(parsed.fingerprint, cookie, database)
    assert key.key_size == 2048

    with pytest.raises(HTTPException) as mismatch:
        await registered_device_public_key(parsed.fingerprint, signer.issue("device", "other", 60), database)
    assert mismatch.value.status_code == 403

    await database.revoke_device_key(parsed.fingerprint)
    with pytest.raises(HTTPException) as revoked:
        await registered_device_public_key(parsed.fingerprint, cookie, database)
    assert revoked.value.status_code == 403