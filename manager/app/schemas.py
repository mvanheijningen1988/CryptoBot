from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from common.models import BotConfig, BotSnapshot


class AgentRegisterRequest(BaseModel):
    agent_id: str
    name: str
    base_url: str
    capacity: int = Field(default=5, ge=1)


class AgentHeartbeatRequest(BaseModel):
    status: str = "online"


class BotCreateRequest(BaseModel):
    name: str
    config: BotConfig


class BotResponse(BaseModel):
    id: str
    name: str
    strategy_type: str
    mode: str
    status: str
    assigned_agent_id: str | None
    config: dict[str, Any]
    latest_metrics: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StartBotRequest(BaseModel):
    agent_id: str | None = None


class UpdateBudgetRequest(BaseModel):
    quote_budget: float = Field(default=0, ge=0)
    base_budget: float = Field(default=0, ge=0)


class BacktestRequest(BaseModel):
    config: BotConfig
    prices: list[float] | None = None


class BacktestResponse(BaseModel):
    initial_equity_quote: float
    final_equity_quote: float
    total_pnl_quote: float
    trades_executed: int


class MetricsPushRequest(BaseModel):
    snapshot: BotSnapshot
