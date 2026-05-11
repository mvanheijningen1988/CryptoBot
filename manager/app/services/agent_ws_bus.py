"""In-memory websocket command bus for manager <-> agent sessions."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class AgentWsSession:
    """One active websocket session from an agent."""

    agent_id: str
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, queue.Queue] = field(default_factory=dict)


_SESSIONS_LOCK = threading.Lock()
_SESSIONS: dict[str, AgentWsSession] = {}


def register_agent_ws_session(agent_id: str, websocket: WebSocket, loop: asyncio.AbstractEventLoop) -> AgentWsSession:
    session = AgentWsSession(agent_id=agent_id, websocket=websocket, loop=loop)
    with _SESSIONS_LOCK:
        _SESSIONS[agent_id] = session
    return session


def unregister_agent_ws_session(agent_id: str, websocket: WebSocket | None = None) -> None:
    with _SESSIONS_LOCK:
        current = _SESSIONS.get(agent_id)
        if not current:
            return
        if websocket is not None and current.websocket is not websocket:
            return
        _SESSIONS.pop(agent_id, None)


def resolve_agent_command_response(agent_id: str, message_id: str, payload: dict | None) -> None:
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(agent_id)
    if not session:
        return

    with session.lock:
        waiter = session.pending.pop(message_id, None)
    if not waiter:
        return

    try:
        waiter.put_nowait(payload if isinstance(payload, dict) else {"ok": False, "message": "invalid_response"})
    except queue.Full:
        pass


def send_agent_command_ws(
    agent_id: str,
    action: str,
    payload: dict,
    timeout_seconds: float = 6.0,
) -> tuple[bool, str, dict | None]:
    """Send one command to an agent over websocket and wait for command_result."""
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(agent_id)
    if not session:
        return False, "no_ws_session", None

    message_id = str(uuid.uuid4())
    waiter: queue.Queue = queue.Queue(maxsize=1)

    with session.lock:
        session.pending[message_id] = waiter

    wire_message = {
        "type": "command",
        "message_id": message_id,
        "payload": {
            "action": str(action or "").strip(),
            **(payload if isinstance(payload, dict) else {}),
        },
    }

    send_coro = session.websocket.send_text(json.dumps(wire_message, separators=(",", ":")))
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_coro,
            session.loop,
        )
        future.result(timeout=min(max(timeout_seconds / 2.0, 0.5), 3.0))
    except Exception as exc:
        # If scheduling fails before the loop takes ownership, close the coroutine
        # to avoid RuntimeWarning: coroutine was never awaited.
        try:
            send_coro.close()
        except Exception:
            pass
        with session.lock:
            session.pending.pop(message_id, None)
        return False, f"ws_send_failed: {exc}", None

    try:
        response = waiter.get(timeout=max(timeout_seconds, 0.5))
    except queue.Empty:
        with session.lock:
            session.pending.pop(message_id, None)
        return False, "ws_timeout", None

    ok = bool(response.get("ok")) if isinstance(response, dict) else False
    message = str((response or {}).get("message", "") or ("ok" if ok else "ws_command_failed"))
    data = (response or {}).get("data") if isinstance(response, dict) else None
    return ok, message, data if isinstance(data, dict) else None
