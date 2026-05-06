"""Bot CRUD, start/stop, budget update, and metrics push endpoints."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from manager.app.database import get_db
from manager.app.models import Agent, Bot
from manager.app.schemas import (
    BotCreateRequest,
    BotResponse,
    MetricsPushRequest,
    StartBotRequest,
    UpdateBudgetRequest,
)
from manager.app.services.agent_client import post_json

router = APIRouter()


def bot_to_response(bot: Bot) -> BotResponse:
    """
    Convert a Bot ORM instance to its Pydantic response schema.

    :param bot: The Bot database model instance.
    :return: A BotResponse Pydantic model.
    """
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
    """
    Return the base URL of an approved agent.

    :param agent_id: Unique identifier of the agent.
    :param db: Database session.
    :return: The agent's base URL string.
    :raises HTTPException: 404 if agent not found, 400 if not approved.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Agent is not approved")
    return agent.base_url


@router.post("/bots", response_model=BotResponse)
def create_bot(payload: BotCreateRequest, db: Session = Depends(get_db)) -> BotResponse:
    """
    Create a new bot with the given configuration (initially stopped).

    :param payload: Bot creation request with name and config.
    :param db: Database session (injected).
    :return: BotResponse for the newly created bot.
    """
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


@router.get("/bots", response_model=list[BotResponse])
def list_bots(db: Session = Depends(get_db)) -> list[BotResponse]:
    """
    Return all bots with their current metrics.

    :param db: Database session (injected).
    :return: List of BotResponse models.
    """
    bots = db.query(Bot).all()
    return [bot_to_response(bot) for bot in bots]


@router.post("/bots/{bot_id}/start")
def start_bot(bot_id: str, payload: StartBotRequest, db: Session = Depends(get_db)) -> dict:
    """
    Start a bot on a specific or auto-selected approved agent.

    :param bot_id: Unique identifier of the bot to start.
    :param payload: Request body with optional agent_id.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 400 if no agent available, 502 on agent failure.
    """
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


@router.post("/bots/{bot_id}/stop")
def stop_bot(bot_id: str, db: Session = Depends(get_db)) -> dict:
    """
    Stop a running bot and notify its assigned agent.

    :param bot_id: Unique identifier of the bot to stop.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 502 on agent failure.
    """
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


@router.post("/bots/{bot_id}/budget")
def update_budget(bot_id: str, payload: UpdateBudgetRequest, db: Session = Depends(get_db)) -> dict:
    """
    Update the budget of a bot and forward the change to its agent if running.

    :param bot_id: Unique identifier of the bot.
    :param payload: Request body with quote_budget and base_budget.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 502 on agent failure.
    """
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


@router.post("/agents/{agent_id}/bots/{bot_id}/metrics")
def push_metrics(agent_id: str, bot_id: str, payload: MetricsPushRequest, db: Session = Depends(get_db)) -> dict:
    """
    Accept a metrics snapshot from an agent for a specific bot.

    :param agent_id: Unique identifier of the reporting agent.
    :param bot_id: Unique identifier of the bot.
    :param payload: Metrics data containing a BotSnapshot.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.latest_metrics_json = payload.snapshot.model_dump_json()
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}
