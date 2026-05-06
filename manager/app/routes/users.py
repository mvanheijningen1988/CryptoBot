"""User management endpoints (admin only)."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.auth import get_current_user, hash_password
from manager.app.database import get_db
from manager.app.models import User

router = APIRouter()


@router.get("/users")
def list_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    """
    List all users (admin only).

    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: List of user dicts.
    :raises HTTPException: 403 if user is not admin.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    users = db.query(User).all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "locale": u.locale, "must_change_password": u.must_change_password}
        for u in users
    ]


@router.post("/users")
def create_user(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """
    Create a new user with the given username, password, and role (admin only).

    :param body: Dict with 'username', 'password', and optional 'role' keys.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with the new user's id, username, and role.
    :raises HTTPException: 403 if not admin, 400/409 on validation errors.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
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


@router.delete("/users/{user_id}")
def delete_user(user_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """
    Delete a user by ID (admin only; cannot delete yourself).

    :param user_id: ID of the user to delete.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 403 if not admin, 404 if user not found, 400 if self-delete.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"ok": True}
