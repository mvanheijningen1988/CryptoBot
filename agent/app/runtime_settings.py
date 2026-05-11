"""In-memory runtime settings cache for the agent service."""
from __future__ import annotations

from copy import deepcopy
from threading import Lock
import os
from typing import Any

import requests

_LOCK = Lock()
_STATE: dict[str, Any] = {"sources": {}, "flat": {}, "exchanges": []}


def apply_runtime_settings(snapshot: dict | None) -> dict:
    """Replace the cached settings snapshot with data from the manager."""
    payload = snapshot if isinstance(snapshot, dict) else {}
    with _LOCK:
        _STATE.clear()
        _STATE.update(
            {
                "sources": payload.get("sources") if isinstance(payload.get("sources"), dict) else {},
                "flat": payload.get("flat") if isinstance(payload.get("flat"), dict) else {},
                "exchanges": payload.get("exchanges") if isinstance(payload.get("exchanges"), list) else [],
            }
        )
        return deepcopy(_STATE)


def refresh_runtime_settings() -> dict:
    """Fetch the latest settings snapshot from the manager."""
    manager_url = str(os.getenv("MANAGER_URL", "http://manager:8000") or "http://manager:8000").rstrip("/")
    agent_id = str(os.getenv("AGENT_ID", "agent") or "agent")
    response = requests.get(f"{manager_url}/api/v1/agents/{agent_id}/settings-runtime", timeout=6)
    response.raise_for_status()
    payload = response.json()
    snapshot = payload.get("settings") if isinstance(payload, dict) else payload
    return apply_runtime_settings(snapshot)


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