"""Pre-trade profitability analysis for static grid configurations."""
from __future__ import annotations

from common import GridConfig


def build_static_grid_profit_preview(grid: GridConfig, fee_rate: float) -> dict:
    """
    Compute per-trade profit for each adjacent level pair in a grid.

    Returns a summary dict indicating whether every trade cycle is
    profitable after accounting for fee_rate (buy + sell fees).

    :param grid: Grid configuration with price range, levels, and order size.
    :param fee_rate: The exchange fee rate per trade (e.g. 0.0025 for 0.25%).
    :return: Dict with profitability summary, step sizes, and per-trade profit stats.
    """
    step = (grid.upper_price - grid.lower_price) / (grid.levels - 1)
    levels = [grid.lower_price + i * step for i in range(grid.levels)]

    per_trade_profits: list[float] = []
    profitable_count = 0

    for i in range(len(levels) - 1):
        buy_price = levels[i]
        sell_price = levels[i + 1]

        quote_spent = grid.order_size_quote
        quote_received_before_fees = quote_spent * (sell_price / buy_price)

        buy_fee = quote_spent * fee_rate
        sell_fee = quote_received_before_fees * fee_rate
        net_profit = quote_received_before_fees - quote_spent - buy_fee - sell_fee

        per_trade_profits.append(net_profit)
        if net_profit > 0:
            profitable_count += 1

    min_profit = min(per_trade_profits)
    max_profit = max(per_trade_profits)
    avg_profit = sum(per_trade_profits) / len(per_trade_profits)

    # If the worst adjacent grid cycle is profitable, the full grid is considered robustly profitable.
    is_profitable = min_profit > 0

    return {
        "is_profitable": is_profitable,
        "step_size": step,
        "step_percent": (step / levels[0]) * 100,
        "profit_per_trade_quote_min": min_profit,
        "profit_per_trade_quote_avg": avg_profit,
        "profit_per_trade_quote_max": max_profit,
        "profitable_trades": profitable_count,
        "total_trade_paths": len(per_trade_profits),
        "fee_rate": fee_rate,
    }
