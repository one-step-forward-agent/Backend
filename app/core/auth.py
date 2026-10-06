from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

password_hash = PasswordHash.recommended()

ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    return bool(hashed) and password_hash.verify(password, hashed)


def _create_token(user_id: int, token_type: str, lifetime: timedelta) -> tuple[str, str, datetime]:
    now = datetime.now(timezone.utc)
    jti, expires_at = uuid4().hex, now + lifetime
    token = jwt.encode(
        {"sub": str(user_id), "type": token_type, "jti": jti, "iat": now, "exp": expires_at},
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return token, jti, expires_at


def create_access_token(user_id: int) -> str:
    return _create_token(user_id, ACCESS_TOKEN, timedelta(minutes=settings.jwt_expire_minutes))[0]


def create_refresh_token(user_id: int) -> tuple[str, str, datetime]:
    return _create_token(user_id, REFRESH_TOKEN, timedelta(days=settings.jwt_refresh_expire_days))


def _decode(token: str, token_type: str) -> dict:
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["sub", "exp", "type", "jti"]},
    )
    if payload.get("type") != token_type:
        raise ValueError("Unexpected token type")
    return payload


def decode_access_token(token: str) -> int:
    return int(_decode(token, ACCESS_TOKEN)["sub"])


def decode_refresh_token(token: str) -> tuple[int, str]:
    payload = _decode(token, REFRESH_TOKEN)
    return int(payload["sub"]), str(payload["jti"])
