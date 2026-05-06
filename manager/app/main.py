from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from manager.app.database import Base, engine, get_db
from manager.app.models import Agent, Bot
from manager.app.schemas import (
    AgentHeartbeatRequest,
    AgentRegisterRequest,
    BacktestRequest,
    BacktestResponse,
    BotCreateRequest,
    BotResponse,
    MetricsPushRequest,
    StartBotRequest,
    UpdateBudgetRequest,
)
from manager.app.services.agent_client import post_json
from manager.app.services.backtest import run_backtest

Base.metadata.create_all(bind=engine)


def _ensure_agent_approval_column() -> None:
    # Lightweight migration for existing SQLite databases created before approval flow.
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            columns = {row[1] for row in rows}
            if "approval_status" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE agents ADD COLUMN approval_status VARCHAR(32) DEFAULT 'pending'"
                )
                conn.exec_driver_sql(
                    "UPDATE agents SET approval_status='approved' WHERE approval_status IS NULL"
                )
                conn.commit()
        except Exception:
            # Best-effort migration: on unsupported dialects or failures, app keeps running.
            pass


_ensure_agent_approval_column()

AGENT_EVENTS: list[dict] = []
AGENT_EVENTS_LOCK = Lock()
MAX_AGENT_EVENTS = 300


def add_agent_event(agent_id: str, agent_name: str, event_type: str, message: str) -> None:
    event = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "event_type": event_type,
        "message": message,
    }
    with AGENT_EVENTS_LOCK:
        AGENT_EVENTS.insert(0, event)
        if len(AGENT_EVENTS) > MAX_AGENT_EVENTS:
            del AGENT_EVENTS[MAX_AGENT_EVENTS:]

app = FastAPI(title="CryptoBot Manager", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def bot_to_response(bot: Bot) -> BotResponse:
    return BotResponse(
        id=bot.id,
        name=bot.name,
        strategy_type=bot.strategy_type,
        mode=bot.mode,
        status=bot.status,
        assigned_agent_id=bot.assigned_agent_id,
        config=json.loads(bot.config_json),
        latest_metrics=json.loads(bot.latest_metrics_json or "{}"),
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


def resolve_agent_url(agent_id: str, db: Session) -> str:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Agent is not approved")
    return agent.base_url


def detach_bots_for_agent(agent: Agent, db: Session) -> None:
    bots = db.query(Bot).filter(Bot.assigned_agent_id == agent.id).all()
    for bot in bots:
        if bot.status == "running":
            post_json(f"{agent.base_url}/agent/bots/{bot.id}/stop", {"bot_id": bot.id})
        bot.status = "stopped"
        bot.assigned_agent_id = None
        bot.updated_at = datetime.utcnow()


@app.get("/")
def root():
    return FileResponse(static_dir / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "manager", "env": os.getenv("ENV", "dev")}


@app.post("/api/agents/register")
def register_agent(payload: AgentRegisterRequest, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == payload.agent_id).first()
    if not agent:
        agent = Agent(
            id=payload.agent_id,
            name=payload.name,
            base_url=payload.base_url,
            capacity=payload.capacity,
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
        if agent.approval_status == "approved":
            agent.status = "online"
        elif agent.approval_status == "rejected":
            agent.status = "rejected"
        else:
            agent.status = "pending"
        agent.last_heartbeat = datetime.utcnow()
    db.commit()
    return {"ok": True, "approval_status": agent.approval_status}


@app.post("/api/agents/{agent_id}/heartbeat")
def heartbeat(agent_id: str, payload: AgentHeartbeatRequest, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.last_heartbeat = datetime.utcnow()
    if agent.approval_status == "approved":
        agent.status = payload.status
    elif agent.approval_status == "rejected":
        agent.status = "rejected"
    else:
        agent.status = "pending"
    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/approve")
def approve_agent(agent_id: str, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.approval_status = "approved"
    agent.status = "online"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "approved", f"Agent {agent.name} was approved.")
    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/reject")
def reject_agent(agent_id: str, db: Session = Depends(get_db)):
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


@app.post("/api/agents/{agent_id}/unapprove")
def unapprove_agent(agent_id: str, db: Session = Depends(get_db)):
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


@app.get("/api/agents")
def list_agents(db: Session = Depends(get_db)):
    agents = db.query(Agent).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "base_url": a.base_url,
            "status": a.status,
            "approval_status": a.approval_status,
            "capacity": a.capacity,
            "last_heartbeat": a.last_heartbeat,
        }
        for a in agents
    ]


@app.get("/api/agent-events")
def list_agent_events():
    with AGENT_EVENTS_LOCK:
        return list(AGENT_EVENTS)


@app.post("/api/bots", response_model=BotResponse)
def create_bot(payload: BotCreateRequest, db: Session = Depends(get_db)):
    bot_id = str(uuid.uuid4())
    bot = Bot(
        id=bot_id,
        name=payload.name,
        strategy_type=payload.config.strategy,
        mode=payload.config.mode,
        status="stopped",
        assigned_agent_id=None,
        config_json=payload.config.model_dump_json(),
        latest_metrics_json="{}",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot_to_response(bot)


@app.get("/api/bots", response_model=list[BotResponse])
def list_bots(db: Session = Depends(get_db)):
    bots = db.query(Bot).all()
    return [bot_to_response(bot) for bot in bots]


@app.post("/api/bots/{bot_id}/start")
def start_bot(bot_id: str, payload: StartBotRequest, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    agent_id = payload.agent_id or bot.assigned_agent_id
    if not agent_id:
        agent = db.query(Agent).filter(Agent.status == "online", Agent.approval_status == "approved").first()
        if not agent:
            raise HTTPException(status_code=400, detail="No approved online agent available")
        agent_id = agent.id

    agent_url = resolve_agent_url(agent_id, db)
    ok, message = post_json(
        f"{agent_url}/agent/bots/{bot.id}/start",
        {
            "bot_id": bot.id,
            "config": json.loads(bot.config_json),
        },
    )
    if not ok:
        raise HTTPException(status_code=502, detail=f"Agent start failed: {message}")

    bot.assigned_agent_id = agent_id
    bot.status = "running"
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/stop")
def stop_bot(bot_id: str, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.assigned_agent_id:
        bot.status = "stopped"
        db.commit()
        return {"ok": True}

    agent_url = resolve_agent_url(bot.assigned_agent_id, db)
    ok, message = post_json(f"{agent_url}/agent/bots/{bot.id}/stop", {"bot_id": bot.id})
    if not ok:
        raise HTTPException(status_code=502, detail=f"Agent stop failed: {message}")

    bot.status = "stopped"
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/budget")
def update_budget(bot_id: str, payload: UpdateBudgetRequest, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    cfg = json.loads(bot.config_json)
    cfg_budget = cfg.get("budget", {})
    cfg_budget["quote_budget"] = payload.quote_budget
    cfg_budget["base_budget"] = payload.base_budget
    cfg["budget"] = cfg_budget
    bot.config_json = json.dumps(cfg)
    bot.updated_at = datetime.utcnow()

    if bot.assigned_agent_id and bot.status == "running":
        agent_url = resolve_agent_url(bot.assigned_agent_id, db)
        ok, message = post_json(
            f"{agent_url}/agent/bots/{bot.id}/budget",
            {
                "bot_id": bot.id,
                "budget": {
                    "quote_budget": payload.quote_budget,
                    "base_budget": payload.base_budget,
                },
            },
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"Agent budget update failed: {message}")

    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/bots/{bot_id}/metrics")
def push_metrics(agent_id: str, bot_id: str, payload: MetricsPushRequest, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.latest_metrics_json = payload.snapshot.model_dump_json()
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/backtest", response_model=BacktestResponse)
def backtest(payload: BacktestRequest):
    result = run_backtest(payload.config, payload.prices)
    return BacktestResponse(**result)
