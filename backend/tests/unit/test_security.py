from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_uses_argon2id_and_verifies_only_matching_password() -> None:
    password_hash = hash_password("Correct Horse Battery Staple!")

    assert password_hash.startswith("$argon2id$")
    assert verify_password("Correct Horse Battery Staple!", password_hash)
    assert not verify_password("incorrect", password_hash)


def test_access_token_contains_only_identity_and_has_short_expiry() -> None:
    settings = Settings(jwt_secret_key="a-test-secret-that-is-long-enough-000000")
    user_id = uuid4()

    token, expires_in = create_access_token(user_id, settings)
    claims = jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )

    assert decode_access_token(token, settings) == user_id
    assert expires_in == 15 * 60
    assert "roles" not in claims
    assert set(claims) == {"sub", "iss", "aud", "iat", "exp", "jti"}


def test_expired_access_token_is_rejected() -> None:
    settings = Settings(jwt_secret_key="a-test-secret-that-is-long-enough-000000")
    now = datetime.now(UTC)
    expired_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now - timedelta(minutes=5),
            "exp": now - timedelta(minutes=1),
            "jti": str(uuid4()),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm="HS256",
    )

    try:
        decode_access_token(expired_token, settings)
    except jwt.ExpiredSignatureError:
        pass
    else:
        raise AssertionError("expired token was accepted")
