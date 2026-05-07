"""Pydantic request/response schemas for the manager REST API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from common import BotConfig, BotSnapshot, GridConfig, RunnerState


class AgentRegisterRequest(BaseModel):
    """Payload sent by an agent to register itself with the manager."""

    agent_id: str
    base_url: str
    capacity: int = Field(default=5, ge=1)
    version: str = ""


class AgentHeartbeatRequest(BaseModel):
    """Periodic keepalive sent by agents."""

    status: str = "online"
    version: str = ""


class BotCreateRequest(BaseModel):
    """Request to create a new trading bot."""

    name: str
    config: BotConfig


class BotResponse(BaseModel):
    """Serialised bot returned by list/detail endpoints."""

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
    """Optional agent override when starting a bot."""

    agent_id: str | None = None


class UpdateBudgetRequest(BaseModel):
    """Hot-update of a bot's quote and base balances."""

    quote_budget: float = Field(default=0, ge=0)
    base_budget: float = Field(default=0, ge=0)


class BacktestRequest(BaseModel):
    """Request to run a back-test with a given bot configuration."""

    config: BotConfig
    prices: list[float] | None = None


class BacktestResponse(BaseModel):
    """Summary results from a back-test run."""

    initial_equity_quote: float
    final_equity_quote: float
    total_pnl_quote: float
    trades_executed: int


class MetricsPushRequest(BaseModel):
    """Snapshot pushed by an agent after each trading loop tick."""

    snapshot: BotSnapshot
    runner_state: RunnerState | None = None
    trade_events: list[dict[str, Any]] = Field(default_factory=list)


class StaticGridPreviewRequest(BaseModel):
    """Request to preview per-trade profitability of a grid configuration."""

    grid: GridConfig
    fee_rate: float = Field(default=0.0025, ge=0, le=0.05)


class GridTradePreview(BaseModel):
    """Per-level trade detail in a grid preview."""

    level: int
    buy_price: float
    sell_price: float
    order_size_quote: float
    net_profit: float
    profitable: bool


class StaticGridPreviewResponse(BaseModel):
    """Per-trade profit breakdown for a static grid."""

    is_profitable: bool
    step_size: float
    step_percent: float
    profit_per_trade_quote_min: float
    profit_per_trade_quote_avg: float
    profit_per_trade_quote_max: float
    profitable_trades: int
    total_trade_paths: int
    fee_rate: float
    levels: list[float]
    trades: list[GridTradePreview]
