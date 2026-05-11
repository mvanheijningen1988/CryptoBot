"""Shared Pydantic models for the CryptoBot system.

Defines bot configuration, grid parameters, budget rules, trade signals,
and the snapshot schema used by agents to report live metrics back to
the manager.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ProfitMode = Literal["withdraw", "compound", "skim"]
"""How unrealised profit is handled: keep (compound), withdraw, or skim a fraction."""


class BudgetConfig(BaseModel):
    """Capital allocation and profit-handling rules for a bot."""

    quote_budget: float = Field(default=0.0, ge=0)
    base_budget: float = Field(default=0.0, ge=0)
    profit_mode: ProfitMode = "compound"
    skim_ratio: float = Field(default=0.5, ge=0, le=1)


class GridConfig(BaseModel):
    """Parameters defining a static grid: price range, number of levels, and order size."""

    lower_price: float = Field(..., gt=0)
    upper_price: float = Field(..., gt=0)
    levels: int = Field(..., ge=2)
    order_size_quote: float = Field(..., gt=0)


class BotConfig(BaseModel):
    """Full configuration for a trading bot instance."""

    market: str
    base_currency: str
    quote_currency: str
    mode: Literal["simulation", "live"] = "simulation"
    strategy: Literal["static_grid"] = "static_grid"
    fee_rate: float = Field(default=0.0, ge=0, le=0.05)
    grid: GridConfig
    budget: BudgetConfig


class TradeSignal(BaseModel):
    """A directional trade instruction emitted by a strategy."""

    side: Literal["buy", "sell"]
    quote_amount: float = Field(..., gt=0)
    level_index: int | None = None


class BotSnapshot(BaseModel):
    """Point-in-time metrics snapshot pushed from an agent to the manager."""

    bot_id: str
    runtime_seconds: int = 0
    timestamp: datetime
    price: float
    quote_balance: float
    base_balance: float
    base_value_in_quote: float
    total_equity_quote: float
    realized_pnl_quote: float
    unrealized_pnl_quote: float
    market_last_price: float = 0.0
    market_open_24h: float = 0.0
    market_change_24h_pct: float = 0.0
    market_volume_24h_quote: float = 0.0
    skimmed_quote: float
    trade_count: int = 0
    status: str


class RunnerState(BaseModel):
    """Serialisable snapshot of a runner's full state for failover.

    Contains everything needed to resume a bot on a different agent:
    strategy state, balances, and cumulative counters.
    """

    level_index: int | None = None
    open_orders: dict[int, str] = Field(default_factory=dict)
    filled_buys: list[int] = Field(default_factory=list)
    filled_amounts: dict[int, float] = Field(default_factory=dict)
    started_at: datetime | None = None
    price: float = 0.0
    quote_balance: float = 0.0
    base_balance: float = 0.0
    initial_equity: float = 0.0
    realized_pnl: float = 0.0
    skimmed_quote: float = 0.0
    trade_count: int = 0
