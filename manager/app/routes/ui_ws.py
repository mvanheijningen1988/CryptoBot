"""WebSocket RPC bridge for manager <-> dashboard UI communication."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import requests
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from manager.app.auth import decode_token
from manager.app.database import SessionLocal
from manager.app.events import wait_for_dashboard_update
from manager.app.models import User

router = APIRouter()


def _extract_token(websocket: WebSocket) -> str:
    token = str(websocket.query_params.get("token", "") or "").strip()
    if token:
        return token
    auth = str(websocket.headers.get("authorization", "") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _is_token_valid(token: str) -> tuple[bool, str | None]:
    if not token:
        return False, None
    try:
        payload = decode_token(token)
    except Exception:
        return False, None

    user_id = str(payload.get("sub", "") or "")
    if not user_id:
        return False, None

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False, None
        return True, user_id
    finally:
        db.close()


def _proxy_http_request(token: str, method: str, path: str, query: dict[str, str], body: Any) -> tuple[int, Any]:
    base_url = os.getenv("MANAGER_INTERNAL_HTTP_URL", "http://127.0.0.1:8000").rstrip("/")
    url = f"{base_url}{path}"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    json_body = body if isinstance(body, (dict, list)) else None

    response = requests.request(
        method=method,
        url=url,
        params=query or None,
        json=json_body,
        headers=headers,
        timeout=20,
    )

    content_type = str(response.headers.get("content-type", "") or "")
    if "application/json" in content_type:
        try:
            payload = response.json()
        except Exception:
            payload = {"detail": response.text or "invalid_json"}
    else:
        payload = response.text
    return response.status_code, payload


async def _dashboard_update_loop(websocket: WebSocket, stop_event: asyncio.Event, send_lock: asyncio.Lock) -> None:
    seq = 0
    while not stop_event.is_set():
        next_seq, event_name, payload = await asyncio.to_thread(
            wait_for_dashboard_update,
            seq,
            15.0,
        )
        if stop_event.is_set():
            return
        if next_seq <= seq:
            continue

        seq = next_seq
        await _send_json_locked(
            websocket,
            {
                "type": "dashboard_update",
                "seq": seq,
                "event": event_name,
                "data": payload if isinstance(payload, dict) else {},
            },
            send_lock,
        )


async def _send_json_locked(websocket: WebSocket, payload: dict, send_lock: asyncio.Lock) -> None:
    async with send_lock:
        await websocket.send_text(json.dumps(payload, separators=(",", ":")))


async def _handle_rpc_message(
    websocket: WebSocket,
    token: str,
    msg: dict,
    send_lock: asyncio.Lock,
) -> None:
    msg_id = str(msg.get("id", "") or "")
    method = str(msg.get("method", "GET") or "GET").upper()
    path = str(msg.get("path", "") or "")
    query = msg.get("query") if isinstance(msg.get("query"), dict) else {}
    body = msg.get("body")

    if not path.startswith("/api/v1/") or path.startswith("/api/v1/ui/ws"):
        await _send_json_locked(
            websocket,
            {"type": "rpc_result", "id": msg_id, "ok": False, "status": 400, "error": "invalid_path"},
            send_lock,
        )
        return

    try:
        status, payload = await asyncio.to_thread(_proxy_http_request, token, method, path, query, body)
    except Exception as exc:
        await _send_json_locked(
            websocket,
            {
                "type": "rpc_result",
                "id": msg_id,
                "ok": False,
                "status": 502,
                "error": f"proxy_failed: {exc}",
            },
            send_lock,
        )
        return

    ok = int(status) < 400
    wire = {
        "type": "rpc_result",
        "id": msg_id,
        "ok": ok,
        "status": int(status),
    }
    if ok:
        wire["data"] = payload
    else:
        if isinstance(payload, dict):
            wire["error"] = str(payload.get("detail", "request_failed") or "request_failed")
            wire["data"] = payload
        else:
            wire["error"] = str(payload or "request_failed")

    await _send_json_locked(websocket, wire, send_lock)


@router.websocket("/ui/ws")
async def ui_ws(websocket: WebSocket) -> None:
    token = _extract_token(websocket)
    token_ok, user_id = _is_token_valid(token)
    if not token_ok:
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept()
    send_lock = asyncio.Lock()
    await _send_json_locked(websocket, {"type": "hello", "user_id": user_id}, send_lock)

    stop_event = asyncio.Event()
    update_task = asyncio.create_task(_dashboard_update_loop(websocket, stop_event, send_lock))
    rpc_tasks: set[asyncio.Task] = set()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await _send_json_locked(websocket, {"type": "error", "error": "invalid_json"}, send_lock)
                continue

            msg_type = str(msg.get("type", "") or "rpc")
            if msg_type != "rpc":
                await _send_json_locked(
                    websocket,
                    {"type": "error", "id": str(msg.get("id", "") or ""), "error": "unknown_type"},
                    send_lock,
                )
                continue

            task = asyncio.create_task(_handle_rpc_message(websocket, token, msg, send_lock))
            rpc_tasks.add(task)
            task.add_done_callback(rpc_tasks.discard)
    except WebSocketDisconnect:
        pass
    finally:
        stop_event.set()
        update_task.cancel()
        for task in list(rpc_tasks):
            task.cancel()
        try:
            await update_task
        except Exception:
            pass
