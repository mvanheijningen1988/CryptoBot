"""Authentication and authorisation helpers.

Provides password hashing (bcrypt), JWT token creation / validation,
FastAPI dependencies for extracting the current user and enforcing
role-based access, and a startup routine that bootstraps the initial
admin account from environment variables.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from manager.app.database import get_db
from manager.app.models import User

# Secret used to sign JWT tokens.  Falls back to a random value so the
# app can start without explicit configuration (tokens won't survive restarts).
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-" + uuid.uuid4().hex)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """
    Return a bcrypt hash of the given plaintext password.

    :param plain: The plaintext password to hash.
    :return: The bcrypt hash string.
    """
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Verify a plaintext password against a bcrypt hash.

    :param plain: The plaintext password to check.
    :param hashed: The stored bcrypt hash.
    :return: True if the password matches, False otherwise.
    """
    return pwd_context.verify(plain, hashed)


def create_token(user_id: str, role: str) -> str:
    """
    Create a signed JWT containing the user's id and role.

    The token expires after ``JWT_EXPIRE_HOURS`` (default 24 h).

    :param user_id: The unique user identifier to embed in the token.
    :param role: The user's role (e.g. 'admin', 'viewer').
    :return: Encoded JWT string.
    """
    payload = {
        "sub": user_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    Decode and validate a JWT.

    :param token: The JWT string to decode.
    :return: The decoded token payload as a dict.
    :raises HTTPException: 401 on expiry or invalidity.
    """
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _extract_token(request: Request) -> str:
    """
    Extract a JWT from the Authorization header or a cookie.

    Checks ``Authorization: Bearer <token>`` first, then falls back
    to a ``token`` cookie.

    :param request: The incoming FastAPI request.
    :return: The extracted JWT string.
    :raises HTTPException: 401 if neither header nor cookie is present.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    token = request.cookies.get("token", "")
    if token:
        return token
    raise HTTPException(status_code=401, detail="Not authenticated")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """
    FastAPI dependency that returns the authenticated User.

    :param request: The incoming FastAPI request.
    :param db: Database session (injected).
    :return: The authenticated User ORM instance.
    :raises HTTPException: 401 if the token is missing, invalid, or user not found.
    """
    token = _extract_token(request)
    payload = decode_token(token)
    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_role(*allowed_roles: str) -> Any:
    """
    Return a FastAPI dependency that restricts access to the given roles.

    Usage::

        @app.get("/admin-only")
        def admin_page(user: User = require_role("admin")):
            ...

    :param allowed_roles: One or more role strings that are permitted access.
    :return: A FastAPI Depends wrapper enforcing the role check.
    """
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return Depends(dependency)


def ensure_admin_user(db: Session) -> None:
    """
    Create or re-sync the admin user from ADMIN_USER / ADMIN_PASSWORD env vars.

    On first run the user is created with must_change_password=True.
    On subsequent runs the stored hash is verified against the env password;
    if they no longer match the password is reset and must_change_password
    is set back to True.

    :param db: Database session to use for querying and persisting the user.
    """
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "")
    if not admin_pass:
        return

    existing = db.query(User).filter(User.username == admin_user).first()
    if existing:
        # Re-sync: if the env password changed or the hash is broken, reset it
        if not verify_password(admin_pass, existing.password_hash):
            existing.password_hash = hash_password(admin_pass)
            existing.must_change_password = True
            db.commit()
        return

    user = User(
        id=str(uuid.uuid4()),
        username=admin_user,
        password_hash=hash_password(admin_pass),
        role="admin",
        locale="en",
        must_change_password=True,
    )
    db.add(user)
    db.commit()
