"""Agent registration, heartbeat, approval, and log-proxy endpoints."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated

import requests
from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.database import get_db
from manager.app.events import add_agent_event
from manager.app.failover import detach_bots_for_agent
from manager.app.models import Agent, Bot
from manager.app.schemas import AgentHeartbeatRequest, AgentRegisterRequest

router = APIRouter()

logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]

_AGENT_NOT_FOUND = "Agent not found"


@router.post("/agents/register")
def register_agent(payload: AgentRegisterRequest, db: DbSession) -> dict:
    """
    Register a new agent or update an existing one's connection details.

    :param payload: Agent registration data (id, name, URL, capacity).
    :param db: Database session (injected).
    :return: Dict with ok status and current approval_status.
    """
    agent = db.query(Agent).filter(Agent.id == payload.agent_id).first()
    if not agent:
        # Check if an agent with the same base_url already exists (e.g. container restart with new UUID)
        agent = db.query(Agent).filter(Agent.base_url == payload.base_url).first()
    if not agent:
        agent = Agent(
            id=payload.agent_id,
            base_url=payload.base_url,
            capacity=payload.capacity,
            version=payload.version,
            status="pending",
            approval_status="pending",
            last_heartbeat=datetime.now(UTC),
        )
        db.add(agent)
        add_agent_event(
            payload.agent_id,
            "discovered",
            f"Agent {payload.agent_id} discovered and awaiting approval.",
        )
        logger.info("Agent %s discovered at %s", payload.agent_id, payload.base_url)
    else:
        agent.id = payload.agent_id
        agent.base_url = payload.base_url
        agent.capacity = payload.capacity
        agent.version = payload.version
        if agent.approval_status == "approved":
            agent.status = "online"
        elif agent.approval_status == "rejected":
            agent.status = "rejected"
        else:
            agent.status = "pending"
        agent.last_heartbeat = datetime.now(UTC)
    db.commit()
    return {"ok": True, "approval_status": agent.approval_status}


@router.post("/agents/{agent_id}/heartbeat", responses={404: {"description": "Agent not found"}})
def heartbeat(agent_id: str, payload: AgentHeartbeatRequest, db: DbSession) -> dict:
    """
    Process a heartbeat from an agent and update its status.

    :param agent_id: Unique identifier of the reporting agent.
    :param payload: Heartbeat data containing agent status.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
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
    return {"ok": True}


@router.post("/agents/{agent_id}/approve", responses={404: {"description": "Agent not found"}})
def approve_agent(agent_id: str, db: DbSession) -> dict:
    """
    Mark an agent as approved so it can receive bot assignments.

    :param agent_id: Unique identifier of the agent to approve.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    agent.approval_status = "approved"
    agent.status = "online"
    agent.last_heartbeat = datetime.now(UTC)
    add_agent_event(agent.id, "approved", f"Agent {agent.id} was approved.")
    logger.info("Agent %s approved", agent.id)
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/reject", responses={404: {"description": "Agent not found"}})
def reject_agent(agent_id: str, db: DbSession) -> dict:
    """
    Reject an agent and detach all its bots.

    :param agent_id: Unique identifier of the agent to reject.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    detach_bots_for_agent(agent, db)
    add_agent_event(agent.id, "rejected", f"Agent {agent.id} was rejected and removed.")
    logger.info("Agent %s rejected and removed", agent.id)
    db.delete(agent)
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/unapprove", responses={404: {"description": "Agent not found"}})
def unapprove_agent(agent_id: str, db: DbSession) -> dict:
    """
    Revoke approval for an agent and detach all its bots.

    :param agent_id: Unique identifier of the agent to un-approve.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    detach_bots_for_agent(agent, db)
    agent.approval_status = "pending"
    agent.status = "pending"
    agent.last_heartbeat = datetime.now(UTC)
    add_agent_event(agent.id, "unapproved", f"Agent {agent.id} was set back to pending.")
    logger.info("Agent %s unapproved", agent.id)
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/stop", responses={404: {"description": "Agent not found"}})
def stop_agent(agent_id: str, db: DbSession) -> dict:
    """
    Stop all bots on an approved agent. The agent remains online.

    :param agent_id: Unique identifier of the agent to stop bots on.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    detach_bots_for_agent(agent, db)
    agent.status = "stopped"
    agent.last_heartbeat = datetime.now(UTC)
    add_agent_event(agent.id, "stopped", f"Bots on agent {agent.id} were stopped.")
    logger.info("Agent %s bots stopped", agent.id)
    db.commit()
    return {"ok": True}


@router.delete("/agents/{agent_id}", responses={404: {"description": "Agent not found"}})
def remove_agent(agent_id: str, db: DbSession) -> dict:
    """
    Remove an agent entirely: detach bots and delete the record.

    :param agent_id: Unique identifier of the agent to remove.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    detach_bots_for_agent(agent, db)
    add_agent_event(agent.id, "removed", f"Agent {agent.id} was removed.")
    logger.info("Agent %s removed", agent.id)
    db.delete(agent)
    db.commit()
    return {"ok": True}


@router.get("/agents")
def list_agents(db: DbSession) -> list[dict]:
    """
    Return all registered agents with their status, approval info, bot count, and bot details.

    :param db: Database session (injected).
    :return: List of agent dicts including bots list per agent.
    """
    agents = db.query(Agent).all()
    all_bots = db.query(Bot).filter(Bot.assigned_agent_id.isnot(None)).all()

    bots_by_agent: dict[str, list[dict]] = {}
    for bot in all_bots:
        metrics = json.loads(bot.latest_metrics_json or "{}")
        config = json.loads(bot.config_json or "{}")
        bots_by_agent.setdefault(bot.assigned_agent_id, []).append({
            "id": bot.id,
            "name": bot.name,
            "status": bot.status,
            "market": config.get("market", "-"),
            "trade_count": metrics.get("trade_count", 0),
            "quote_balance": metrics.get("quote_balance", 0),
            "base_balance": metrics.get("base_balance", 0),
        })

    return [
        {
            "id": a.id,
            "base_url": a.base_url,
            "status": a.status,
            "approval_status": a.approval_status,
            "capacity": a.capacity,
            "version": a.version,
            "uptime_seconds": a.uptime_seconds,
            "last_heartbeat": a.last_heartbeat.isoformat() + "Z" if a.last_heartbeat else None,
            "bot_count": len(bots_by_agent.get(a.id, [])),
            "bots": bots_by_agent.get(a.id, []),
        }
        for a in agents
    ]


@router.get("/agent-events")
def list_agent_events() -> list[dict]:
    """
    Return the in-memory agent event log (most recent first).

    :return: List of agent event dicts.
    """
    from manager.app.events import AGENT_EVENTS, AGENT_EVENTS_LOCK

    with AGENT_EVENTS_LOCK:
        return list(AGENT_EVENTS)


@router.get("/agents/{agent_id}/logs", responses={400: {"description": "Agent not approved"}, 404: {"description": "Agent not found"}, 502: {"description": "Proxy failure"}})
def get_agent_logs(
    agent_id: str,
    db: DbSession,
    limit: int = 200,
    bot_id: str | None = None,
    category: str | None = None,
) -> dict:
    """
    Proxy log retrieval from an approved agent, forwarding filters.

    :param agent_id: Unique identifier of the agent to query.
    :param limit: Maximum number of log entries (1-1000).
    :param bot_id: Optional filter by bot ID.
    :param category: Optional filter by log category.
    :param db: Database session (injected).
    :return: Agent's log response (proxied JSON).
    :raises HTTPException: 404 if agent not found, 400 if not approved, 502 on proxy failure.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Only approved agent logs are available")

    safe_limit = max(1, min(limit, 1000))
    query_params: dict = {"limit": safe_limit}
    if bot_id:
        query_params["bot_id"] = bot_id
    if category:
        query_params["category"] = category

    try:
        response = requests.get(
            f"{agent.base_url}/agent/logs",
            params=query_params,
            timeout=6,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch agent logs: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Agent returned {response.status_code}: {response.text}")

    return response.json()
