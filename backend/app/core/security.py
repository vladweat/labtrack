"""Password hashing and short-lived JWT primitives."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

_password_hasher = PasswordHasher()
_JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    """Hash a password with Argon2id and a generated salt."""

    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password without exposing malformed hashes to callers."""

    try:
        return _password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def create_access_token(user_id: UUID, settings: Settings) -> tuple[str, int]:
    """Issue a short-lived token containing identity, never authorization grants."""

    now = datetime.now(UTC)
    expires_in = settings.jwt_access_token_minutes * 60
    payload = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": str(uuid4()),
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=_JWT_ALGORITHM
    )
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> UUID:
    """Validate signature and standard claims, then return the token subject."""

    payload = jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=[_JWT_ALGORITHM],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        options={"require": ["sub", "iss", "aud", "iat", "exp", "jti"]},
    )
    return UUID(payload["sub"])
