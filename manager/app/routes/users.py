"""User management endpoints (admin only)."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.auth import get_current_user, hash_password
from manager.app.database import get_db
from manager.app.models import User

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

_ADMIN_ONLY = "Admin only"


@router.get("/users", responses={403: {"description": "Admin only"}})
def list_users(user: CurrentUser, db: DbSession) -> list[dict]:
    """
    List all users (admin only).

    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: List of user dicts.
    :raises HTTPException: 403 if user is not admin.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    users = db.query(User).all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "locale": u.locale, "must_change_password": u.must_change_password}
        for u in users
    ]


@router.post("/users", responses={400: {"description": "Validation error"}, 403: {"description": "Admin only"}, 409: {"description": "Conflict"}})
def create_user(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """
    Create a new user with the given username, password, and role (admin only).

    :param body: Dict with 'username', 'password', and optional 'role' keys.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with the new user's id, username, and role.
    :raises HTTPException: 403 if not admin, 400/409 on validation errors.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "viewer")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if role not in ("admin", "moderator", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    new_user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
        locale="en",
        must_change_password=True,
    )
    db.add(new_user)
    db.commit()
    return {"id": new_user.id, "username": new_user.username, "role": new_user.role}


@router.delete("/users/{user_id}", responses={400: {"description": "Cannot delete yourself"}, 403: {"description": "Admin only"}, 404: {"description": "Not found"}})
def delete_user(user_id: str, user: CurrentUser, db: DbSession) -> dict:
    """
    Delete a user by ID (admin only; cannot delete yourself).

    :param user_id: ID of the user to delete.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 403 if not admin, 404 if user not found, 400 if self-delete.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"ok": True}
