"""Authentication endpoints: login, profile, password change, locale."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.auth import (
    JWT_EXPIRE_HOURS,
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from manager.app.database import get_db
from manager.app.models import User

import re

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

# Minimum password requirements
_PW_MIN_LENGTH = 8
_PW_DIGIT_RE = re.compile(r"\d")
_PW_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def _validate_password(pw: str) -> str | None:
    """Return an error message if *pw* violates password rules, else None."""
    if len(pw) < _PW_MIN_LENGTH:
        return f"Password must be at least {_PW_MIN_LENGTH} characters"
    if not _PW_DIGIT_RE.search(pw):
        return "Password must contain at least 1 digit"
    if not _PW_SPECIAL_RE.search(pw):
        return "Password must contain at least 1 special character"
    return None


@router.post("/auth/login", responses={401: {"description": "Invalid credentials"}})
def auth_login(body: dict, db: DbSession) -> dict:
    """
    Authenticate a user by username/password and return a JWT.

    :param body: Dict with 'username' and 'password' keys.
    :param db: Database session (injected).
    :return: Dict with token and user profile.
    :raises HTTPException: 401 if credentials are invalid.
    """
    username = body.get("username", "")
    password = body.get("password", "")
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user.id, user.role)
    return {
        "token": token,
        "session_max_seconds": JWT_EXPIRE_HOURS * 3600,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "locale": user.locale,
            "must_change_password": user.must_change_password,
        },
    }


@router.get("/auth/me")
def auth_me(user: CurrentUser) -> dict:
    """
    Return the profile of the currently authenticated user.

    :param user: The authenticated user (injected).
    :return: Dict with user id, username, role, locale, and must_change_password.
    """
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "locale": user.locale,
        "must_change_password": user.must_change_password,
    }


@router.post("/auth/change-password", responses={400: {"description": "Invalid password"}})
def change_password(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """
    Update the authenticated user's password and clear the forced-change flag.

    :param body: Dict with 'new_password' key.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 400 if password does not meet requirements.
    """
    new_password = body.get("new_password", "")
    error = _validate_password(new_password)
    if error:
        raise HTTPException(status_code=400, detail=error)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}


@router.post("/auth/locale", responses={400: {"description": "Unsupported locale"}})
def update_locale(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """
    Persist the user's preferred locale (en or nl).

    :param body: Dict with 'locale' key.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status and saved locale.
    :raises HTTPException: 400 if locale is unsupported.
    """
    locale = body.get("locale", "en")
    if locale not in ("en", "nl"):
        raise HTTPException(status_code=400, detail="Unsupported locale")
    user.locale = locale
    db.commit()
    return {"ok": True, "locale": locale}
