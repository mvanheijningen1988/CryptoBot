"""In-memory runtime settings cache for the manager service."""
from __future__ import annotations

from copy import deepcopy
from threading import Lock
import os

from sqlalchemy.orm import Session

from manager.app.services.settings_store import build_runtime_snapshot, ensure_settings_seeded

_LOCK = Lock()
_STATE: dict[str, object] = {"sources": {}, "flat": {}, "exchanges": []}


def refresh_runtime_settings(db: Session) -> dict:
    """Reload settings from the database and update the in-memory snapshot."""
    ensure_settings_seeded(db)
    snapshot = build_runtime_snapshot(db)
    with _LOCK:
        _STATE.clear()
        _STATE.update(snapshot)
        return deepcopy(_STATE)


def get_runtime_snapshot() -> dict:
    with _LOCK:
        return deepcopy(_STATE)


def get_setting(key: str, default: str = "") -> str:
    with _LOCK:
        value = (_STATE.get("flat") or {}).get(key)
    if value is None or str(value).strip() == "":
        return str(os.getenv(key, default) or default)
    return str(value)


def get_int(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default))))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_exchange(name: str = "bitvavo") -> dict:
    target = str(name or "").strip().lower()
    with _LOCK:
        exchanges = list((_STATE.get("exchanges") or []))
    for exchange in exchanges:
        if str(exchange.get("key", "") or "").strip().lower() == target:
            return deepcopy(exchange)
        if str(exchange.get("name", "") or "").strip().lower() == target:
            return deepcopy(exchange)
    return {}