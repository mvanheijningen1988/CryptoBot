"""Database-backed settings endpoints (admin only)."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.auth import get_current_user
from manager.app.database import get_db
from manager.app.models import Agent, User
from manager.app.services.agent_ws_bus import send_agent_command_ws
from manager.app.services.runtime_settings import refresh_runtime_settings
from manager.app.services.settings_store import (
    build_runtime_snapshot,
    build_settings_payload,
    create_exchange,
    delete_exchange,
    ensure_settings_seeded,
    update_exchange,
    update_settings,
)

router = APIRouter(prefix="/settings", tags=["settings"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]

_ADMIN_ONLY = "Admin only"


@router.get("")
def get_settings(user: CurrentUser, db: DbSession) -> dict:
    """Return grouped editable settings from database (admin only)."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)

    ensure_settings_seeded(db)
    payload = build_settings_payload(db)
    payload["runtime"] = build_runtime_snapshot(db)
    return payload


@router.post("")
def save_settings(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """Persist updated scalar settings values in database (admin only)."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)

    updates = body.get("items") if isinstance(body, dict) else None
    if not isinstance(updates, list) or not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    saved = update_settings(db, updates)
    runtime = refresh_runtime_settings(db)
    _refresh_agents(db)

    return {
        "ok": True,
        "saved": saved,
        "message": "Settings saved in database.",
        "runtime": runtime,
    }


@router.post("/exchanges")
def create_exchange_setting(body: dict, user: CurrentUser, db: DbSession) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    ensure_settings_seeded(db)
    row = create_exchange(db, body if isinstance(body, dict) else {})
    runtime = refresh_runtime_settings(db)
    _refresh_agents(db)
    return {"ok": True, "item": row, "runtime": runtime}


@router.put("/exchanges/{exchange_id}")
def update_exchange_setting(exchange_id: int, body: dict, user: CurrentUser, db: DbSession) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    ensure_settings_seeded(db)
    row = update_exchange(db, exchange_id, body if isinstance(body, dict) else {})
    runtime = refresh_runtime_settings(db)
    _refresh_agents(db)
    return {"ok": True, "item": row, "runtime": runtime}


@router.delete("/exchanges/{exchange_id}")
def delete_exchange_setting(exchange_id: int, user: CurrentUser, db: DbSession) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    ensure_settings_seeded(db)
    delete_exchange(db, exchange_id)
    runtime = refresh_runtime_settings(db)
    _refresh_agents(db)
    return {"ok": True, "runtime": runtime}


@router.get("/runtime")
def get_runtime_settings(user: CurrentUser, db: DbSession) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY)
    ensure_settings_seeded(db)
    return {"settings": build_runtime_snapshot(db)}


def _refresh_agents(db: Session) -> None:
    snapshot = build_runtime_snapshot(db)
    agents = (
        db.query(Agent)
        .filter(Agent.approval_status == "approved", Agent.status.in_(["online", "stopped", "pending"]))
        .all()
    )
    for agent in agents:
        send_agent_command_ws(agent.id, "refresh_settings", {"settings": snapshot})
