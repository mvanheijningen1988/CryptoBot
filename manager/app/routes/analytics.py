"""Backtesting and grid profitability preview endpoints."""
from __future__ import annotations

from fastapi.routing import APIRouter

from manager.app.schemas import (
    BacktestRequest,
    BacktestResponse,
    StaticGridPreviewRequest,
    StaticGridPreviewResponse,
)
from manager.app.services.backtest import run_backtest
from manager.app.services.grid_preview import build_static_grid_profit_preview

router = APIRouter()


@router.post("/backtest")
def backtest(payload: BacktestRequest) -> BacktestResponse:
    """
    Run a quick backtest with the supplied configuration and price data.

    :param payload: Backtest request with config and optional prices.
    :return: BacktestResponse with equity and trade stats.
    """
    result = run_backtest(payload.config, payload.prices)
    return BacktestResponse(**result)


@router.post("/strategy/static-grid/preview")
def static_grid_preview(payload: StaticGridPreviewRequest) -> StaticGridPreviewResponse:
    """
    Return a profitability preview for the given static grid parameters.

    :param payload: Grid preview request with grid config and fee_rate.
    :return: StaticGridPreviewResponse with profitability stats.
    """
    result = build_static_grid_profit_preview(payload.grid, payload.fee_rate)
    return StaticGridPreviewResponse(**result)
