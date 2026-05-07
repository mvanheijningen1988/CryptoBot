"""Agent HTTP route handlers for bot lifecycle and log retrieval."""
from __future__ import annotations

import logging

from fastapi import HTTPException
from fastapi.routing import APIRouter

from agent.app.config import AGENT_ID, runner_manager
from agent.app.schemas import BudgetPayload, StartBotPayload, StopBotPayload
from agent.app.version import __version__

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """
    Health check endpoint.

    :return: Dict with service status, name, version, and agent ID.
    """
    return {"status": "ok", "service": "agent", "version": __version__, "agent_id": AGENT_ID}


@router.post("/agent/bots/{bot_id}/start")
def start_bot(bot_id: str, payload: StartBotPayload) -> dict:
    """
    Start a trading bot with the given configuration.

    :param bot_id: Unique identifier of the bot to start.
    :param payload: Request body containing bot_id and config.
    :return: Acknowledgement dict.
    """
    logger.info("START request for bot %s (mode=%s, market=%s)", bot_id, payload.config.mode, payload.config.market)
    try:
        runner_manager.start_bot(bot_id, payload.config, runner_state=payload.runner_state)
    except Exception as exc:
        logger.exception("Failed to start bot %s", bot_id)
        raise HTTPException(status_code=500, detail=f"Bot start failed: {exc}") from exc
    logger.info("Bot %s started successfully", bot_id)
    return {"ok": True}


@router.post("/agent/bots/{bot_id}/stop")
def stop_bot(bot_id: str, payload: StopBotPayload) -> dict:
    """
    Stop a running trading bot.

    :param bot_id: Unique identifier of the bot to stop.
    :param payload: Request body containing bot_id.
    :return: Acknowledgement dict.
    """
    logger.info("STOP request for bot %s", bot_id)
    try:
        runner_manager.stop_bot(bot_id)
    except Exception as exc:
        logger.exception("Failed to stop bot %s", bot_id)
        raise HTTPException(status_code=500, detail=f"Bot stop failed: {exc}") from exc
    logger.info("Bot %s stopped successfully", bot_id)
    return {"ok": True}


@router.post("/agent/bots/{bot_id}/budget")
def update_budget(bot_id: str, payload: BudgetPayload) -> dict:
    """
    Update the budget for a running bot.

    :param bot_id: Unique identifier of the bot.
    :param payload: Request body containing bot_id and new budget.
    :return: Acknowledgement dict.
    """
    logger.info("BUDGET update for bot %s", bot_id)
    try:
        runner_manager.update_budget(bot_id, payload.budget)
    except Exception as exc:
        logger.exception("Failed to update budget for bot %s", bot_id)
        raise HTTPException(status_code=500, detail=f"Budget update failed: {exc}") from exc
    return {"ok": True}


@router.get("/agent/bots")
def list_bots() -> list:
    """
    List all bots managed by this agent.

    :return: List of dicts with bot_id and running status.
    """
    return runner_manager.list_bots()


@router.get("/agent/logs")
def list_logs(limit: int = 200, bot_id: str | None = None, category: str | None = None) -> dict:
    """
    Return recent agent/bot log entries.

    :param limit: Maximum number of log entries to return (1-1000).
    :param bot_id: Optional filter by bot ID.
    :param category: Optional filter by log category.
    :return: Dict with agent info and filtered log entries.
    """
    safe_limit = max(1, min(limit, 1000))
    return {
        "agent_id": AGENT_ID,
        "logs": runner_manager.get_logs(limit=safe_limit, bot_id=bot_id, category=category),
    }


@router.get("/agent/bots/{bot_id}/open-orders")
def get_open_orders(bot_id: str) -> dict:
    """Return open grid orders for a running bot."""
    runner = runner_manager.runners.get(bot_id)
    if not runner or not runner.running:
        raise HTTPException(status_code=404, detail="Bot not running")
    orders = runner.strategy.get_open_orders(runner.state)
    return {"bot_id": bot_id, "orders": orders}
