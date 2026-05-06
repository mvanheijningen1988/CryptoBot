"""FastAPI application for the CryptoBot agent service.

Exposes endpoints for bot lifecycle management (start/stop/budget), log
retrieval, and a health check.  On startup the agent registers itself
with the manager and launches a background heartbeat thread.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import requests
from fastapi import FastAPI
from pydantic import BaseModel

from agent.app.runner import RunnerManager
from common.models import BotConfig, BudgetConfig

MANAGER_URL = os.getenv("MANAGER_URL", "http://manager:8000")
AGENT_NAME = os.getenv("AGENT_NAME", "agent-1")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8100"))
AGENT_ID = os.getenv("AGENT_ID", str(uuid.uuid4()))
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", f"http://agent:{AGENT_PORT}")

app = FastAPI(title="CryptoBot Agent", version="0.1.0")
runner_manager = RunnerManager(MANAGER_URL, AGENT_ID)


class StartBotPayload(BaseModel):
    """Request body for starting a bot on this agent."""

    bot_id: str
    config: BotConfig


class StopBotPayload(BaseModel):
    """Request body for stopping a running bot."""

    bot_id: str


class BudgetPayload(BaseModel):
    """Request body for updating a bot's budget."""

    bot_id: str
    budget: BudgetConfig


def register_agent() -> bool:
    """
    Register this agent with the manager service.

    :return: True if registration succeeded, False otherwise.
    """
    payload = {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "base_url": AGENT_BASE_URL,
        "capacity": 10,
    }
    try:
        response = requests.post(f"{MANAGER_URL}/api/agents/register", json=payload, timeout=5)
        if response.status_code < 400:
            runner_manager.log_system("register_ok", "Agent registered to manager.", {"manager_url": MANAGER_URL})
        else:
            runner_manager.log_system(
                "register_failed",
                "Agent registration returned an error.",
                {"status_code": response.status_code, "body": response.text[:300]},
            )
        return response.status_code < 400
    except requests.RequestException:
        runner_manager.log_system("register_failed", "Agent registration request failed.")
        return False


def heartbeat_loop() -> None:
    """
    Periodically ping the manager to confirm this agent is alive.

    Runs indefinitely in a daemon thread, re-registering if the
    manager loses track of the agent.
    """
    registered = False
    last_state: str | None = None
    while True:
        if not registered:
            registered = register_agent()

        try:
            response = requests.post(
                f"{MANAGER_URL}/api/agents/{AGENT_ID}/heartbeat",
                json={"status": "online"},
                timeout=5,
            )
            if response.status_code == 404:
                registered = False
            state = "online" if response.status_code < 400 else f"heartbeat_error_{response.status_code}"
            if state != last_state:
                runner_manager.log_system(
                    "heartbeat_state",
                    "Agent heartbeat state changed.",
                    {"state": state},
                )
                last_state = state
        except requests.RequestException:
            registered = False
            if last_state != "heartbeat_unreachable":
                runner_manager.log_system("heartbeat_state", "Manager heartbeat endpoint unreachable.")
                last_state = "heartbeat_unreachable"
        time.sleep(10)


@app.on_event("startup")
def startup_event() -> None:
    """
    Register agent and start the heartbeat loop on application start.
    """
    runner_manager.log_system("agent_startup", "Agent process started.", {"agent_id": AGENT_ID, "agent_name": AGENT_NAME})
    register_agent()
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()


@app.get("/health")
def health() -> dict:
    """
    Health check endpoint.

    :return: Dict with service status, name, and agent ID.
    """
    return {"status": "ok", "service": "agent", "agent_id": AGENT_ID}


@app.post("/agent/bots/{bot_id}/start")
def start_bot(bot_id: str, payload: StartBotPayload) -> dict:
    """
    Start a trading bot with the given configuration.

    :param bot_id: Unique identifier of the bot to start.
    :param payload: Request body containing bot_id and config.
    :return: Acknowledgement dict.
    """
    runner_manager.start_bot(bot_id, payload.config)
    return {"ok": True}


@app.post("/agent/bots/{bot_id}/stop")
def stop_bot(bot_id: str, payload: StopBotPayload) -> dict:
    """
    Stop a running trading bot.

    :param bot_id: Unique identifier of the bot to stop.
    :param payload: Request body containing bot_id.
    :return: Acknowledgement dict.
    """
    runner_manager.stop_bot(bot_id)
    return {"ok": True}


@app.post("/agent/bots/{bot_id}/budget")
def update_budget(bot_id: str, payload: BudgetPayload) -> dict:
    """
    Update the budget for a running bot.

    :param bot_id: Unique identifier of the bot.
    :param payload: Request body containing bot_id and new budget.
    :return: Acknowledgement dict.
    """
    runner_manager.update_budget(bot_id, payload.budget)
    return {"ok": True}


@app.get("/agent/bots")
def list_bots() -> list:
    """
    List all bots managed by this agent.

    :return: List of dicts with bot_id and running status.
    """
    return runner_manager.list_bots()


@app.get("/agent/logs")
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
        "agent_name": AGENT_NAME,
        "logs": runner_manager.get_logs(limit=safe_limit, bot_id=bot_id, category=category),
    }
