"""
JWT Authentication & timing-safe credential verification.

Validates login credentials strictly against environment variables (`APP_USERNAME`
and `APP_PASSWORD`) using `hmac.compare_digest` to prevent timing side-channels,
then mints signed HS256 JWT tokens with explicit `iat` and `exp` claims.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from backend.config import Settings, get_settings
from backend.monitoring import capture_error

bearer_scheme = HTTPBearer(auto_error=False)


def verify_credentials(username: str, password: str, settings: Settings) -> bool:
    """
    Verify username & password against configured environment credentials.

    Fails closed (returns False) if `APP_USERNAME` or `APP_PASSWORD` is empty so
    unconfigured deployments can never accept blank logins.
    """
    expected_user = settings.app_username
    expected_pass = settings.app_password
    if not expected_user or not expected_pass:
        return False

    user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return user_ok and pass_ok


def create_access_token(username: str, settings: Settings) -> tuple[str, int]:
    """
    Generate a signed JWT token for the authenticated user.

    Returns `(encoded_jwt, expires_in_seconds)`.
    """
    now = datetime.now(timezone.utc)
    expires_delta = timedelta(minutes=settings.jwt_expire_minutes)
    expire_at = now + expires_delta

    payload: Dict[str, Any] = {
        "sub": username,
        "role": "arena_operator",
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str, settings: Settings) -> Dict[str, Any]:
    """Decode and validate a JWT string, raising HTTP 401 on expiry or tampering."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat"]},
        )
        return payload
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT token has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InvalidTokenError as exc:
        capture_error(exc, component="auth", operation="decode_access_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency that enforces Bearer JWT authentication on protected routes."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials, settings)
    username = str(payload.get("sub", "")).strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing subject claim.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username
