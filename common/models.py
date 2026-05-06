from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ProfitMode = Literal["compound", "skim"]


class BudgetConfig(BaseModel):
    quote_budget: float = Field(default=0.0, ge=0)
    base_budget: float = Field(default=0.0, ge=0)
    profit_mode: ProfitMode = "compound"
    skim_ratio: float = Field(default=0.5, ge=0, le=1)


class GridConfig(BaseModel):
    lower_price: float = Field(..., gt=0)
    upper_price: float = Field(..., gt=0)
    levels: int = Field(..., ge=2)
    order_size_quote: float = Field(..., gt=0)


class BotConfig(BaseModel):
    market: str
    base_currency: str
    quote_currency: str
    mode: Literal["simulation", "live"] = "simulation"
    strategy: Literal["static_grid"] = "static_grid"
    start_price: float = Field(default=100.0, gt=0)
    grid: GridConfig
    budget: BudgetConfig


class TradeSignal(BaseModel):
    side: Literal["buy", "sell"]
    quote_amount: float = Field(..., gt=0)


class BotSnapshot(BaseModel):
    bot_id: str
    timestamp: datetime
    price: float
    quote_balance: float
    base_balance: float
    base_value_in_quote: float
    total_equity_quote: float
    realized_pnl_quote: float
    unrealized_pnl_quote: float
    skimmed_quote: float
    status: str
