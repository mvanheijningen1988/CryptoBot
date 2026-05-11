"""Bidirectional websocket ingest channel between manager and agents."""
from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from manager.app.database import SessionLocal
from manager.app.events import add_agent_event, publish_dashboard_update
from manager.app.models import Agent
from manager.app.routes.bots import ingest_agent_metrics
from manager.app.schemas import AgentHeartbeatRequest, MetricsPushRequest
from manager.app.services.agent_ws_bus import (
    register_agent_ws_session,
    resolve_agent_command_response,
    unregister_agent_ws_session,
)
from manager.app.services.runtime_settings import get_setting

router = APIRouter()


def _ws_auth_ok(websocket: WebSocket) -> bool:
    """Validate optional shared websocket token for agent connections."""
    expected = get_setting("AGENT_WS_TOKEN", "").strip()
    if not expected:
        return True
    received = websocket.headers.get("x-agent-ws-token", "").strip()
    return bool(received) and received == expected


def _update_agent_heartbeat(agent_id: str, payload: AgentHeartbeatRequest) -> None:
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            return
        agent.last_heartbeat = datetime.now(UTC)
        if payload.version:
            agent.version = payload.version
        agent.uptime_seconds = payload.uptime_seconds
        if agent.approval_status == "approved" and agent.status != "stopped":
            agent.status = payload.status
        elif agent.approval_status == "rejected":
            agent.status = "rejected"
        else:
            agent.status = "pending"
        db.commit()
    finally:
        db.close()


@router.websocket("/agents/ws")
async def agent_ws(websocket: WebSocket) -> None:
    """Accept agent websocket sessions for heartbeat and bot metrics ingest."""
    agent_id = str(websocket.query_params.get("agent_id", "") or "").strip()
    if not agent_id:
        await websocket.close(code=1008, reason="missing agent_id")
        return

    if not _ws_auth_ok(websocket):
        await websocket.close(code=1008, reason="invalid token")
        return

    await websocket.accept()
    register_agent_ws_session(agent_id=agent_id, websocket=websocket, loop=asyncio.get_running_loop())
    add_agent_event(agent_id, "ws_connected", f"Agent {agent_id} websocket connected.")
    publish_dashboard_update("agent_ws_connected", {"agent_id": agent_id})

    try:
        await websocket.send_text(json.dumps({"type": "hello", "agent_id": agent_id}))
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "error": "invalid_json"}))
                continue

            msg_type = str(message.get("type", "") or "").strip().lower()
            msg_id = str(message.get("message_id", "") or "")
            try:
                if msg_type == "heartbeat":
                    payload = AgentHeartbeatRequest(**(message.get("payload") or {}))
                    _update_agent_heartbeat(agent_id, payload)
                    await websocket.send_text(json.dumps({"type": "ack", "message_id": msg_id, "event": "heartbeat"}))
                    continue

                if msg_type == "agent_event":
                    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    event_type = str(payload.get("event_type", "status") or "status")
                    msg = str(payload.get("message", "") or "")
                    add_agent_event(agent_id, event_type, msg or f"Agent {agent_id} event via websocket")
                    await websocket.send_text(json.dumps({"type": "ack", "message_id": msg_id, "event": "agent_event"}))
                    continue

                if msg_type == "command_result":
                    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    resolve_agent_command_response(agent_id=agent_id, message_id=msg_id, payload=payload)
                    continue

                if msg_type == "bot_metrics":
                    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    bot_id = str(payload.get("bot_id", "") or "")
                    metrics_payload = MetricsPushRequest(
                        snapshot=payload.get("snapshot"),
                        runner_state=payload.get("runner_state"),
                        trade_events=payload.get("trade_events") or [],
                    )
                    if not bot_id:
                        raise HTTPException(status_code=400, detail="missing bot_id")
                    db = SessionLocal()
                    try:
                        ingest_agent_metrics(agent_id=agent_id, bot_id=bot_id, payload=metrics_payload, db=db)
                    finally:
                        db.close()
                    await websocket.send_text(json.dumps({"type": "ack", "message_id": msg_id, "event": "bot_metrics", "bot_id": bot_id}))
                    continue

                await websocket.send_text(json.dumps({"type": "error", "message_id": msg_id, "error": "unknown_type"}))
            except HTTPException as exc:
                await websocket.send_text(json.dumps({"type": "error", "message_id": msg_id, "error": str(exc.detail)}))
            except Exception as exc:
                await websocket.send_text(json.dumps({"type": "error", "message_id": msg_id, "error": str(exc)}))
    except WebSocketDisconnect:
        pass
    finally:
        unregister_agent_ws_session(agent_id=agent_id, websocket=websocket)
        add_agent_event(agent_id, "ws_disconnected", f"Agent {agent_id} websocket disconnected.")
        publish_dashboard_update("agent_ws_disconnected", {"agent_id": agent_id})
