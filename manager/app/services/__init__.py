"""Manager service modules: agent client, backtesting, and grid preview."""

from manager.app.services.agent_client import post_json
from manager.app.services.backtest import run_backtest
from manager.app.services.grid_preview import build_static_grid_profit_preview

__all__ = ["post_json", "run_backtest", "build_static_grid_profit_preview"]
