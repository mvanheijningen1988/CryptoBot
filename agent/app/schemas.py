"""Pydantic request schemas for agent endpoints."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from common import BotConfig, BudgetConfig, RunnerState


class StartBotPayload(BaseModel):
    """Request body for starting a bot on this agent."""

    bot_id: str
    config: BotConfig
    runner_state: RunnerState | None = None


class StopBotPayload(BaseModel):
    """Request body for stopping a running bot."""

    bot_id: str


class BudgetPayload(BaseModel):
    """Request body for updating a bot's budget."""

    bot_id: str
    budget: BudgetConfig


DeleteBotMode = Literal[
    "delete_open_orders",
    "delete_as_is",
    "transform_to_base",
    "transform_to_quote",
]


class DeleteBotPayload(BaseModel):
    """Request body for preparing a bot for deletion."""

    bot_id: str
    delete_mode: DeleteBotMode = "delete_open_orders"
