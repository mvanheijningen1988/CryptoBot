"""Agent registration, heartbeat, approval, and log-proxy endpoints."""
from __future__ import annotations

from datetime import datetime

import requests
from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy import func
from sqlalchemy.orm import Session

from manager.app.database import get_db
from manager.app.events import add_agent_event
from manager.app.failover import detach_bots_for_agent
from manager.app.models import Agent, Bot
from manager.app.schemas import AgentHeartbeatRequest, AgentRegisterRequest

router = APIRouter()


@router.post("/agents/register")
def register_agent(payload: AgentRegisterRequest, db: Session = Depends(get_db)) -> dict:
    """
    Register a new agent or update an existing one's connection details.

    :param payload: Agent registration data (id, name, URL, capacity).
    :param db: Database session (injected).
    :return: Dict with ok status and current approval_status.
    """
    agent = db.query(Agent).filter(Agent.id == payload.agent_id).first()
    if not agent:
        agent = Agent(
            id=payload.agent_id,
            name=payload.name,
            base_url=payload.base_url,
            capacity=payload.capacity,
            version=payload.version,
            status="pending",
            approval_status="pending",
            last_heartbeat=datetime.utcnow(),
        )
        db.add(agent)
        add_agent_event(
            payload.agent_id,
            payload.name,
            "discovered",
            f"Agent {payload.name} discovered and awaiting approval.",
        )
    else:
        agent.name = payload.name
        agent.base_url = payload.base_url
        agent.capacity = payload.capacity
        agent.version = payload.version
        if agent.approval_status == "approved":
            agent.status = "online"
        elif agent.approval_status == "rejected":
            agent.status = "rejected"
        else:
            agent.status = "pending"
        agent.last_heartbeat = datetime.utcnow()
    db.commit()
    return {"ok": True, "approval_status": agent.approval_status}


@router.post("/agents/{agent_id}/heartbeat")
def heartbeat(agent_id: str, payload: AgentHeartbeatRequest, db: Session = Depends(get_db)) -> dict:
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
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.last_heartbeat = datetime.utcnow()
    if payload.version:
        agent.version = payload.version
    if agent.approval_status == "approved":
        agent.status = payload.status
    elif agent.approval_status == "rejected":
        agent.status = "rejected"
    else:
        agent.status = "pending"
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/approve")
def approve_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    """
    Mark an agent as approved so it can receive bot assignments.

    :param agent_id: Unique identifier of the agent to approve.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.approval_status = "approved"
    agent.status = "online"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "approved", f"Agent {agent.name} was approved.")
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/reject")
def reject_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    """
    Reject an agent and detach all its bots.

    :param agent_id: Unique identifier of the agent to reject.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    detach_bots_for_agent(agent, db)
    agent.approval_status = "rejected"
    agent.status = "rejected"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "rejected", f"Agent {agent.name} was rejected.")
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/unapprove")
def unapprove_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    """
    Revoke approval for an agent and detach all its bots.

    :param agent_id: Unique identifier of the agent to un-approve.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    detach_bots_for_agent(agent, db)
    agent.approval_status = "pending"
    agent.status = "pending"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "unapproved", f"Agent {agent.name} was set back to pending.")
    db.commit()
    return {"ok": True}


@router.get("/agents")
def list_agents(db: Session = Depends(get_db)) -> list[dict]:
    """
    Return all registered agents with their status, approval info, and bot count.

    :param db: Database session (injected).
    :return: List of agent dicts including bot_count per agent.
    """
    agents = db.query(Agent).all()
    bot_counts: dict[str, int] = {}
    rows = db.query(Bot.assigned_agent_id, func.count(Bot.id)).filter(
        Bot.assigned_agent_id.isnot(None),
        Bot.status == "running",
    ).group_by(Bot.assigned_agent_id).all()
    for agent_id, cnt in rows:
        bot_counts[agent_id] = cnt
    return [
        {
            "id": a.id,
            "name": a.name,
            "base_url": a.base_url,
            "status": a.status,
            "approval_status": a.approval_status,
            "capacity": a.capacity,
            "version": a.version,
            "last_heartbeat": a.last_heartbeat,
            "bot_count": bot_counts.get(a.id, 0),
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


@router.get("/agents/{agent_id}/logs")
def get_agent_logs(
    agent_id: str,
    limit: int = 200,
    bot_id: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
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
        raise HTTPException(status_code=404, detail="Agent not found")
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
