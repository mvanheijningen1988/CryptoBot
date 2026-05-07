"""Bot CRUD, start/stop, budget update, and metrics push endpoints."""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from manager.app.database import get_db
from manager.app.events import (
    add_equity_point,
    add_trade_event,
    delete_trade_events_for_bot,
    get_trade_events,
)
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

DbSession = Annotated[Session, Depends(get_db)]

_BOT_NOT_FOUND = "Bot not found"
_AGENT_NOT_FOUND = "Agent not found"
_AGENT_NOT_APPROVED = "Agent is not approved"
_ACTIVE_BOT_STATUSES = ("initializing", "running")


def _resolve_fee_rate_for_bot(bot: Bot) -> float:
    """Return the configured fee rate for the bot mode."""
    if bot.mode == "simulation":
        return float(os.getenv("SIM_FEE_RATE", "0.0025"))
    return float(os.getenv("LIVE_FEE_RATE", os.getenv("SIM_FEE_RATE", "0.0025")))


def _build_pair_metrics(bot: Bot, event: object, linked: object | None) -> dict | None:
    """Compute realized grid PnL for a linked buy/sell fill pair."""
    if linked is None:
        return None
    if getattr(event, "event_type", None) != "order_filled" or getattr(linked, "event_type", None) != "order_filled":
        return None

    if getattr(event, "side", None) == "buy" and getattr(linked, "side", None) == "sell":
        buy_event, sell_event = event, linked
    elif getattr(event, "side", None) == "sell" and getattr(linked, "side", None) == "buy":
        buy_event, sell_event = linked, event
    else:
        return None

    buy_price = float(getattr(buy_event, "price", 0) or 0)
    sell_price = float(getattr(sell_event, "price", 0) or 0)
    quote_spent = float(getattr(buy_event, "quote_amount", 0) or 0)
    if buy_price <= 0 or sell_price <= 0 or quote_spent <= 0:
        return None

    fee_rate = _resolve_fee_rate_for_bot(bot)
    quantity_base = quote_spent / buy_price
    gross_profit = (sell_price - buy_price) * quantity_base
    quote_received_before_fees = quote_spent * (sell_price / buy_price)
    buy_fee = quote_spent * fee_rate
    sell_fee = quote_received_before_fees * fee_rate
    total_fees = buy_fee + sell_fee
    realized_pnl = gross_profit - total_fees

    return {
        "quantity_base": round(quantity_base, 8),
        "gross_profit_quote": round(gross_profit, 6),
        "total_fees_quote": round(total_fees, 6),
        "realized_pnl_quote": round(realized_pnl, 6),
        "fee_rate": fee_rate,
    }


def _select_least_loaded_agent(db: Session) -> Agent | None:
    """Pick the approved online agent with the fewest assigned bots.

    Only agents whose current bot count is below their capacity are
    considered.  Among those, the agent with the fewest bots wins
    (ties broken by agent id for determinism).

    :param db: Database session.
    :return: The best agent, or ``None`` if none are available.
    """
    from sqlalchemy import func

    agents = (
        db.query(Agent)
        .filter(Agent.status == "online", Agent.approval_status == "approved")
        .all()
    )
    if not agents:
        return None

    # Count running bots per agent
    counts = dict(
        db.query(Bot.assigned_agent_id, func.count(Bot.id))
        .filter(Bot.status.in_(_ACTIVE_BOT_STATUSES), Bot.assigned_agent_id.isnot(None))
        .group_by(Bot.assigned_agent_id)
        .all()
    )

    best: Agent | None = None
    best_count = float("inf")
    for agent in agents:
        n = counts.get(agent.id, 0)
        if n >= agent.capacity:
            continue
        if n < best_count or (n == best_count and best and agent.id < best.id):
            best = agent
            best_count = n
    return best


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
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail=_AGENT_NOT_APPROVED)  # NOSONAR - documented on calling routes
    return agent.base_url


@router.post("/bots", responses={400: {"description": "Agent not approved"}, 404: {"description": "Agent not found"}})
def create_bot(payload: BotCreateRequest, db: DbSession) -> BotResponse:
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
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot_to_response(bot)


@router.get("/bots")
def list_bots(db: DbSession) -> list[BotResponse]:
    """
    Return all bots with their current metrics.

    :param db: Database session (injected).
    :return: List of BotResponse models.
    """
    bots = db.query(Bot).all()
    return [bot_to_response(bot) for bot in bots]


@router.post("/bots/{bot_id}/start", responses={400: {"description": "No agent available"}, 404: {"description": "Not found"}, 502: {"description": "Agent failure"}})
def start_bot(bot_id: str, payload: StartBotRequest, db: DbSession) -> dict:
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
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    agent_id = payload.agent_id or bot.assigned_agent_id
    if not agent_id:
        agent = _select_least_loaded_agent(db)
        if not agent:
            raise HTTPException(status_code=400, detail="No approved online agent available")
        agent_id = agent.id

    agent_url = resolve_agent_url(agent_id, db)
    logger.info("Starting bot %s on agent %s (%s)", bot.id, agent_id, agent_url)
    start_payload: dict = {
        "bot_id": bot.id,
        "config": json.loads(bot.config_json),
    }
    # Include saved runner state so the agent can resume from last position
    saved_state = bot.state_json or "{}"
    if saved_state and saved_state != "{}":
        start_payload["runner_state"] = json.loads(saved_state)

    ok, message = post_json(
        f"{agent_url}/agent/bots/{bot.id}/start",
        start_payload,
    )
    if not ok:
        logger.error("Agent %s failed to start bot %s: %s", agent_id, bot.id, message)
        raise HTTPException(status_code=502, detail=f"Agent start failed: {message}")

    bot.assigned_agent_id = agent_id
    bot.status = "initializing"
    bot.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Bot %s now initializing on agent %s", bot.id, agent_id)
    return {"ok": True}


@router.post("/bots/{bot_id}/stop", responses={404: {"description": "Not found"}})
def stop_bot(bot_id: str, db: DbSession) -> dict:
    """
    Stop a running bot and notify its assigned agent.

    If the agent is unreachable or missing, the bot is force-stopped
    and unassigned so it doesn't remain in a stuck "running" state.

    :param bot_id: Unique identifier of the bot to stop.
    :param db: Database session (injected).
    :return: Dict with ok status and optional warning.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    warning = None

    if bot.assigned_agent_id:
        agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
        if agent and agent.approval_status == "approved":
            ok, message = post_json(
                f"{agent.base_url}/agent/bots/{bot.id}/stop",
                {"bot_id": bot.id},
            )
            if not ok:
                logger.warning(
                    "Could not reach agent %s to stop bot %s: %s – force-stopping",
                    bot.assigned_agent_id, bot.id, message,
                )
                warning = f"Agent unreachable, bot force-stopped: {message}"
        else:
            warning = "Agent not found or not approved, bot force-stopped"

    bot.status = "stopped"
    bot.assigned_agent_id = None
    bot.updated_at = datetime.now(UTC)
    db.commit()
    result: dict = {"ok": True}
    if warning:
        result["warning"] = warning
    return result


@router.delete("/bots/{bot_id}", responses={404: {"description": "Not found"}, 409: {"description": "Bot is running"}})
def delete_bot(bot_id: str, db: DbSession) -> dict:
    """
    Delete a stopped bot and its associated event data.

    :param bot_id: Unique identifier of the bot to delete.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 409 if bot is still running.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)
    if bot.status in _ACTIVE_BOT_STATUSES:
        raise HTTPException(status_code=409, detail="Cannot delete an active bot – stop it first")
    # Clean up trade events and equity history
    delete_trade_events_for_bot(bot_id)
    from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK
    with EQUITY_HISTORY_LOCK:
        EQUITY_HISTORY.pop(bot_id, None)
    db.delete(bot)
    db.commit()
    logger.info("Bot %s deleted", bot_id)
    return {"ok": True}


@router.post("/bots/{bot_id}/budget", responses={404: {"description": "Not found"}, 502: {"description": "Agent failure"}})
def update_budget(bot_id: str, payload: UpdateBudgetRequest, db: DbSession) -> dict:
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
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    cfg = json.loads(bot.config_json)
    cfg_budget = cfg.get("budget", {})
    cfg_budget["quote_budget"] = payload.quote_budget
    cfg_budget["base_budget"] = payload.base_budget
    cfg["budget"] = cfg_budget
    bot.config_json = json.dumps(cfg)
    bot.updated_at = datetime.now(UTC)

    if bot.assigned_agent_id and bot.status in _ACTIVE_BOT_STATUSES:
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


@router.post("/agents/{agent_id}/bots/{bot_id}/metrics", responses={404: {"description": "Bot not found"}})
def push_metrics(agent_id: str, bot_id: str, payload: MetricsPushRequest, db: DbSession) -> dict:
    """
    Accept a metrics snapshot from an agent for a specific bot.

    Records the equity data-point for the budget trend chart and
    generates trade events when the trade count increases.

    :param agent_id: Unique identifier of the reporting agent.
    :param bot_id: Unique identifier of the bot.
    :param payload: Metrics data containing a BotSnapshot.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    snapshot = payload.snapshot

    # ── Record equity history for budget trend chart ──
    add_equity_point(
        bot_id,
        snapshot.timestamp.isoformat(),
        snapshot.total_equity_quote,
        snapshot.price,
    )

    # ── Detect new trades and emit trade events ──
    config = json.loads(bot.config_json or "{}")
    market = config.get("market", "")
    if payload.trade_events:
        # Use detailed events from the agent
        for ev in payload.trade_events:
            add_trade_event(
                bot_id=bot_id,
                bot_name=bot.name,
                side=ev.get("side", "trade"),
                quote_amount=ev.get("quote_amount", 0),
                price=ev.get("price", snapshot.price),
                trade_pnl=ev.get("trade_pnl", 0),
                total_equity=ev.get("total_equity", snapshot.total_equity_quote),
                trade_number=ev.get("trade_number", snapshot.trade_count),
                event_type=ev.get("event_type", "trade"),
                level_index=ev.get("level_index"),
                market=market,
                order_id=ev.get("order_id"),
            )
    else:
        # Fallback: infer from trade_count change
        prev_metrics = json.loads(bot.latest_metrics_json or "{}")
        prev_trade_count = prev_metrics.get("trade_count", 0)
        if snapshot.trade_count > prev_trade_count:
            new_trades = snapshot.trade_count - prev_trade_count
            prev_equity = prev_metrics.get("total_equity_quote", snapshot.total_equity_quote)
            trade_pnl = (snapshot.total_equity_quote - prev_equity) / max(new_trades, 1)
            for i in range(new_trades):
                add_trade_event(
                    bot_id=bot_id,
                    bot_name=bot.name,
                    side="trade",
                    quote_amount=0,
                    price=snapshot.price,
                    trade_pnl=trade_pnl,
                    total_equity=snapshot.total_equity_quote,
                    trade_number=prev_trade_count + i + 1,
                    market=market,
                )

    bot.status = snapshot.status
    bot.latest_metrics_json = snapshot.model_dump_json()
    if payload.runner_state:
        bot.state_json = payload.runner_state.model_dump_json()
    bot.updated_at = datetime.now(UTC)
    db.commit()
    return {"ok": True}


@router.get("/trade-events")
def list_trade_events(bot_id: str | None = None) -> list[dict]:
    """Return trade events from the database (most recent first)."""
    return get_trade_events(bot_id=bot_id)


@router.get("/trade-events/{event_id}")
def get_single_trade_event(event_id: str) -> dict:
    """Return a single trade event by ID, including linked order details."""
    from manager.app.database import SessionLocal
    from manager.app.models import TradeEvent as TE
    db = SessionLocal()
    try:
        ev = db.query(TE).filter(TE.id == event_id).first()
        if not ev:
            raise HTTPException(status_code=404, detail="Trade event not found")
        result = {
            "id": ev.id,
            "order_id": ev.order_id,
            "timestamp": ev.timestamp.isoformat() + "Z" if ev.timestamp else "",
            "bot_id": ev.bot_id,
            "bot_name": ev.bot_name,
            "market": ev.market or "",
            "event_type": ev.event_type,
            "order_id": ev.order_id,
            "side": ev.side,
            "quote_amount": ev.quote_amount,
            "price": ev.price,
            "trade_pnl": ev.trade_pnl,
            "total_equity": ev.total_equity,
            "trade_number": ev.trade_number,
            "level_index": ev.level_index,
            "linked_order_id": ev.linked_order_id,
            "linked_order": None,
            "pair_metrics": None,
        }
        if ev.linked_order_id:
            linked = db.query(TE).filter(TE.id == ev.linked_order_id).first()
            if linked:
                result["linked_order"] = {
                    "id": linked.id,
                    "order_id": linked.order_id,
                    "timestamp": linked.timestamp.isoformat() + "Z" if linked.timestamp else "",
                    "event_type": linked.event_type,
                    "side": linked.side,
                    "quote_amount": linked.quote_amount,
                    "price": linked.price,
                    "trade_pnl": linked.trade_pnl,
                    "level_index": linked.level_index,
                }
                bot = db.query(Bot).filter(Bot.id == ev.bot_id).first()
                if bot:
                    result["pair_metrics"] = _build_pair_metrics(bot, ev, linked)
        return result
    finally:
        db.close()


@router.get("/bots/{bot_id}/equity-history")
def get_equity_history(bot_id: str, db: DbSession) -> dict:
    """Return equity data-points and budget info for the trend chart.

    :param bot_id: The bot to fetch history for.
    :param db: Database session (injected).
    :return: Dict with points list and budget metadata.
    """
    from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK

    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    config = json.loads(bot.config_json) if bot else {}
    budget = config.get("budget", {})
    starting_budget = budget.get("quote_budget", 0)
    metrics = json.loads(bot.latest_metrics_json or "{}") if bot else {}

    with EQUITY_HISTORY_LOCK:
        points = EQUITY_HISTORY.get(bot_id, [])

    return {
        "points": points,
        "starting_budget": starting_budget,
        "total_equity": metrics.get("total_equity_quote", starting_budget),
        "pnl": metrics.get("unrealized_pnl_quote", 0),
    }


@router.get("/bots/equity-history/total")
def get_total_equity_history(db: DbSession) -> dict:
    """Return combined equity data-points across all bots."""
    from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK

    bots = db.query(Bot).all()
    total_starting_budget = 0.0
    total_equity = 0.0
    total_pnl = 0.0

    for bot in bots:
        config = json.loads(bot.config_json) if bot else {}
        budget = config.get("budget", {})
        total_starting_budget += budget.get("quote_budget", 0)
        metrics = json.loads(bot.latest_metrics_json or "{}") if bot else {}
        total_equity += metrics.get("total_equity_quote", budget.get("quote_budget", 0))
        total_pnl += metrics.get("unrealized_pnl_quote", 0)

    # Merge all bot equity histories by timestamp
    with EQUITY_HISTORY_LOCK:
        all_series = {bid: list(pts) for bid, pts in EQUITY_HISTORY.items()}

    # Build a combined timeline: for each timestamp sum the values
    ts_map: dict[str, float] = {}
    for pts in all_series.values():
        for p in pts:
            ts_map[p["t"]] = ts_map.get(p["t"], 0) + p["v"]

    points = [{"t": t, "v": v, "p": 0} for t, v in sorted(ts_map.items())]

    return {
        "points": points,
        "starting_budget": total_starting_budget,
        "total_equity": total_equity,
        "pnl": total_pnl,
    }


@router.get("/bots/{bot_id}/open-orders")
def get_open_orders(bot_id: str, db: DbSession) -> dict:
    """Proxy open orders from the agent running this bot."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)
    if bot.status != "running" or not bot.assigned_agent_id:
        raise HTTPException(status_code=409, detail="Bot is not running")
    agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    import requests as req
    try:
        resp = req.get(f"{agent.base_url}/agent/bots/{bot_id}/open-orders", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
