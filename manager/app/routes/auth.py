"""Authentication endpoints: login, profile, password change, locale."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.auth import (
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from manager.app.database import get_db
from manager.app.models import User

router = APIRouter()


@router.post("/auth/login")
def auth_login(body: dict, db: Session = Depends(get_db)) -> dict:
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
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "locale": user.locale,
            "must_change_password": user.must_change_password,
        },
    }


@router.get("/auth/me")
def auth_me(user: User = Depends(get_current_user)) -> dict:
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


@router.post("/auth/change-password")
def change_password(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """
    Update the authenticated user's password and clear the forced-change flag.

    :param body: Dict with 'new_password' key.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 400 if password is shorter than 6 characters.
    """
    new_password = body.get("new_password", "")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}


@router.post("/auth/locale")
def update_locale(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
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
